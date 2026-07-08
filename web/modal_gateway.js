/**
 * Modal Gateway — Extension JS pour ComfyUI
 * ==========================================
 * Ajoute un sélecteur de GPU dans la barre d'outils et redirige
 * "Queue Prompt" vers l'API Modal quand le mode cloud est actif.
 *
 * @version 1.0.0
 * @see https://github.com/caru-ini/modal-comfyui
 */
(function () {
    'use strict';

    // ─── Configuration ───────────────────────────────────────────────────────

    const CONFIG = {
        /**
         * URL de base de l'API Modal Gateway.
         * Chargée depuis localStorage ou l'API serveur.
         * @type {string}
         */
        API_URL: localStorage.getItem('modal-api-url') || '',

        /** Clé de stockage localStorage pour persister le choix du GPU. */
        STORAGE_KEY: 'modal-gateway-mode',

        /** Timeout par défaut pour les requêtes vers Modal (en ms). */
        TIMEOUT_MS: 300000, // 5 minutes — workflows longs (FLUX, vidéo, cold start)

        /** Intervalle entre les tentatives d'interception de l'API (en ms). */
        RETRY_INTERVAL: 800,

        /** Nombre maximum de tentatives d'interception. */
        MAX_RETRIES: 20,

        /**
         * Clé API pour l'authentification auprès du Gateway Modal.
         * Chargée depuis localStorage ou l'API serveur.
         */
        API_KEY: localStorage.getItem('modal-api-key') || '',
    };

    // ─── Chargement de la config depuis le serveur au démarrage ────────────

    fetch('/api/modal/config')
        .then(function (r) { return r.json(); })
        .then(function (config) {
            if (config.api_url) {
                CONFIG.API_URL = config.api_url;
                localStorage.setItem('modal-api-url', config.api_url);
            }
            if (config.api_key) {
                CONFIG.API_KEY = config.api_key;
                localStorage.setItem('modal-api-key', config.api_key);
            }
        })
        .catch(function () {
            // Mode dégradé : utiliser les valeurs par défaut (vides ou localStorage)
            // L'utilisateur devra configurer via la modale
        });

    /**
     * Options de GPU proposées dans le dropdown.
     * Chaque entrée : { value, label }.
     * - "local" = exécution locale (pas d'interception).
     * - "modal:<gpu>" = exécution sur le GPU Modal spécifié.
     */
    const GPU_OPTIONS = [
        { value: 'local', label: '🖥️  Local (gratuit)' },
        { value: 'modal:l4', label: '☁️  L4 — $0.80/h' },
        { value: 'modal:l40s', label: '☁️  L40S — $1.95/h' },
        { value: 'modal:a100', label: '☁️  A100 80GB — $2.50/h' },
        { value: 'modal:h100', label: '☁️  H100 — $3.95/h' },
    ];

    /** Variable pour stocker le GPU actif lors d'un envoi cloud. */
    var currentGpu = '';

    /**
     * Nom de l'extension enregistrée auprès de ComfyUI.
     * Utilisé par app.registerExtension().
     */
    const EXTENSION_NAME = 'Modal Gateway';

    /**
     * Flag indiquant si le dropdown a déjà été injecté dans la nouvelle barre d'outils.
     */
    var dropdownInjected = false;

    // ─── Helpers de transfert d'images ─────────────────────────────────────

    /**
     * Convertit un Blob en chaîne base64 (sans préfixe data:).
     * @param {Blob} blob
     * @returns {Promise<string>}
     */
    function blobToBase64(blob) {
        return new Promise(function (resolve, reject) {
            var reader = new FileReader();
            reader.onload = function () {
                var result = reader.result;
                // data:image/png;base64,XXXXX → XXXXX
                var comma = result.indexOf(',');
                resolve(comma >= 0 ? result.substring(comma + 1) : result);
            };
            reader.onerror = reject;
            reader.readAsDataURL(blob);
        });
    }

    /**
     * Scanne le workflow pour trouver les nodes LoadImage avec des fichiers
     * locaux, les charge depuis le serveur ComfyUI et les encode en base64
     * dans extra_data.images.
     *
     * Si une image encodée dépasse ~4 MB, on bascule vers l'upload direct
     * via /upload/image.
     *
     * @param {object} workflow - Le workflow JSON à enrichir.
     * @returns {Promise<object>} Le workflow enrichi (ou l'original inchangé).
     */
    async function encodeLocalImages(workflow) {
        var nodesWithImages = [];

        for (var nodeId in workflow) {
            if (Object.prototype.hasOwnProperty.call(workflow, nodeId)) {
                var node = workflow[nodeId];
                if (node.class_type === 'LoadImage' && node.inputs && node.inputs.image) {
                    var filename = node.inputs.image;
                    if (typeof filename === 'string' && !filename.startsWith('http')) {
                        nodesWithImages.push({ nodeId: nodeId, node: node, filename: filename });
                    }
                }
            }
        }

        if (nodesWithImages.length === 0) return workflow;

        var MAX_BASE64_SIZE = 4 * 1024 * 1024; // 4 MB

        for (var i = 0; i < nodesWithImages.length; i++) {
            var item = nodesWithImages[i];
            var nodeId = item.nodeId;
            var node = item.node;
            var filename = item.filename;

            try {
                var response = await fetch(
                    '/view?filename=' + encodeURIComponent(filename) + '&type=input&subfolder='
                );
                if (!response.ok) {
                    console.warn('Modal Gateway: fichier local introuvable:', filename);
                    continue;
                }

                var blob = await response.blob();
                var base64 = await blobToBase64(blob);

                if (base64.length > MAX_BASE64_SIZE) {
                    // Trop gros pour base64 → upload direct
                    console.warn('Modal Gateway: image trop grande pour base64, upload direct:', filename);
                    var uploadResult = await uploadImageDirectly(blob, filename);
                    if (uploadResult && uploadResult.name) {
                        node.inputs.image = uploadResult.name;
                    }
                } else {
                    // Encodage base64 dans extra_data
                    if (!workflow.extra_data) workflow.extra_data = {};
                    if (!workflow.extra_data.images) workflow.extra_data.images = [];
                    workflow.extra_data.images.push({
                        filename: filename,
                        subfolder: '',
                        type: 'input',
                        imagebase64: base64,
                    });
                    // On conserve le nom de fichier original comme référence
                    node.inputs.image = filename;
                }
            } catch (e) {
                console.warn('Modal Gateway: impossible de charger l\'image locale:', filename, e);
            }
        }

        return workflow;
    }

    /**
     * Upload direct d'une image (blob) vers l'endpoint /upload/image du worker Modal.
     * Utilisé en fallback quand l'image est trop grosse pour le base64.
     * @param {Blob} blob - Données binaires de l'image.
     * @param {string} filename - Nom de fichier original.
     * @returns {Promise<object>} Réponse JSON du serveur (contient .name).
     */
    async function uploadImageDirectly(blob, filename) {
        var formData = new FormData();
        formData.append('image', blob, filename);
        formData.append('overwrite', 'true');

        var gpuParam = currentGpu ? '?gpu=' + encodeURIComponent(currentGpu) : '';

        var response = await fetch(CONFIG.API_URL + '/upload/image' + gpuParam, {
            method: 'POST',
            headers: {
                'X-API-Key': CONFIG.API_KEY,
            },
            body: formData,
        });

        if (!response.ok) {
            var errText = '';
            try { errText = await response.text(); } catch (_) {}
            throw new Error('Upload failed: HTTP ' + response.status + ' ' + errText);
        }

        return await response.json();
    }

    // ─── Injection du dropdown via l'API d'extension ComfyUI ────────────────

    /**
     * Injecte le sélecteur de GPU dans la nouvelle barre d'outils de ComfyUI.
     * Utilise la même technique que FR.IA : on trouve le bouton Settings
     * (toujours présent dans le nouveau menu) via app.menu.settingsGroup.element,
     * puis on insère notre conteneur juste avant.
     *
     * Cette méthode fonctionne avec ComfyUI v1.33+ (nouveau menu PrimeVue)
     * et évite de chercher l'ancien .comfy-menu qui est masqué.
     *
     * @param {object} appInstance - L'instance ComfyApp (window.app ou window.comfyAPI.app.app).
     */
    function injectDropdownInToolbar(appInstance) {
        if (dropdownInjected) return;

        var settingsButton = appInstance && appInstance.menu && appInstance.menu.settingsGroup
            ? appInstance.menu.settingsGroup.element
            : null;

        if (!settingsButton) {
            console.warn('Modal Gateway: bouton Settings non trouvé dans app.menu.settingsGroup.element', {
                hasMenu: !!(appInstance && appInstance.menu),
                menuKeys: appInstance && appInstance.menu ? Object.keys(appInstance.menu) : [],
            });
            // Réessayer plus tard (le menu peut ne pas encore être monté)
            setTimeout(function () {
                injectDropdownInToolbar(appInstance);
            }, 300);
            return;
        }

        console.log('Modal Gateway: bouton Settings trouvé, injection du dropdown avant', {
            buttonTag: settingsButton.tagName,
            buttonText: settingsButton.textContent || settingsButton.innerHTML?.substring(0, 50),
            parentTag: settingsButton.parentNode ? settingsButton.parentNode.tagName : 'null',
        });

        // Conteneur principal
        const container = document.createElement('div');
        container.id = 'modal-gateway-container';
        container.style.cssText = [
            'display: inline-flex',
            'align-items: center',
            'margin: 0 4px',
            'gap: 4px',
            'user-select: none',
        ].join(';') + ';';

        // Icône/label
        const label = document.createElement('span');
        label.textContent = '🎮';
        label.title = 'Mode de rendu — choisissez Local ou un GPU cloud';
        label.setAttribute('aria-label', 'Mode de rendu');

        // Élément <select>
        const select = document.createElement('select');
        select.id = 'modal-gpu-selector';
        select.setAttribute('aria-label', 'Sélection du GPU');
        select.style.cssText = [
            'background: #2a2a2a',
            'color: #e0e0e0',
            'border: 1px solid #555',
            'border-radius: 4px',
            'padding: 4px 8px',
            'font-size: 13px',
            'font-family: sans-serif',
            'cursor: pointer',
            'outline: none',
        ].join(';') + ';';

        GPU_OPTIONS.forEach(function (opt) {
            const option = document.createElement('option');
            option.value = opt.value;
            option.textContent = opt.label;
            select.appendChild(option);
        });

        // Restaurer le choix sauvegardé dans localStorage
        const saved = localStorage.getItem(CONFIG.STORAGE_KEY);
        if (saved && [...select.options].some(function (o) { return o.value === saved; })) {
            select.value = saved;
        }

        // Persister le choix à chaque changement
        select.addEventListener('change', function () {
            localStorage.setItem(CONFIG.STORAGE_KEY, select.value);
            // Notifier l'utilisateur du mode choisi
            const mode = select.value;
            if (mode !== 'local') {
                const gpu = mode.split(':')[1].toUpperCase();
                showNotification('☁️  Mode cloud actif — ' + gpu, 'info');
            }
        });

        // Bouton paramètres ⚙️
        const settingsBtn = document.createElement('button');
        settingsBtn.id = 'modal-settings-btn';
        settingsBtn.innerHTML = '⚙️';
        settingsBtn.title = 'Paramètres Modal Gateway';
        settingsBtn.style.cssText = 'background:none;border:none;cursor:pointer;font-size:16px;padding:4px;color:#aaa;';
        settingsBtn.addEventListener('click', openSettingsModal);

        container.appendChild(label);
        container.appendChild(select);
        container.appendChild(settingsBtn);

        // Insérer avant le bouton Settings (comme FR.IA)
        settingsButton.parentNode.insertBefore(container, settingsButton);

        dropdownInjected = true;
        console.log('Modal Gateway: dropdown injecté dans la barre d\'outils (avant le bouton Settings)', {
            parentTag: settingsButton.parentNode.tagName,
            containerHTML: container.outerHTML.substring(0, 200),
        });
    }

    // ─── Interception de Queue Prompt ───────────────────────────────────────

    /**
     * Cherche l'objet API ComfyUI dans plusieurs emplacements possibles.
     * Selon la version de ComfyUI, l'API peut se trouver à différents endroits :
     * - window.api (ancienne version)
     * - window.comfyAPI.api (ComfyUI v1.30+)
     * - window.comfyAPI.app.app.api (ComfyUI v1.30+, chemin alternatif)
     * - window.comfyapp.api (autre contexte)
     * @returns {object|null} L'objet API s'il expose queuePrompt, sinon null.
     */
    function findApi() {
        var sources = [
            { obj: window.api, name: 'window.api' },
            { obj: window.comfyAPI && window.comfyAPI.api, name: 'window.comfyAPI.api' },
            { obj: window.comfyAPI && window.comfyAPI.app && window.comfyAPI.app.app && window.comfyAPI.app.app.api, name: 'window.comfyAPI.app.app.api' },
            { obj: window['comfyapp'] && window['comfyapp'].api, name: 'window.comfyapp.api' },
        ];

        for (var i = 0; i < sources.length; i++) {
            var source = sources[i];
            if (source.obj && typeof source.obj.queuePrompt === 'function') {
                console.log('Modal Gateway: API trouvée via ' + source.name);
                return source.obj;
            }
        }
        return null;
    }

    /**
     * Patche queuePrompt pour intercepter les envois de workflow.
     * Utilise findApi() pour localiser l'API ComfyUI quel que soit l'emplacement.
     * En mode "local", le comportement d'origine est conservé.
     * En mode "modal:*", le workflow est sérialisé et envoyé à l'API Modal.
     */
    function interceptQueuePrompt() {
        var api = findApi();

        if (!api) {
            // L'API ComfyUI n'est pas encore trouvée — on réessaie plus tard
            return false;
        }

        // Réinitialiser le compteur de tentatives si on a enfin trouvé l'API
        if (window._modalGatewayRetryCount !== undefined) {
            window._modalGatewayRetryCount = 0;
        }

        var originalQueue = api.queuePrompt.bind(api);

        api.queuePrompt = async function (number, workflow, options) {
            var select = document.getElementById('modal-gpu-selector');
            var mode = select ? select.value : 'local';

            // ── Mode local : comportement normal ──
            if (mode === 'local') {
                return originalQueue(number, workflow, options);
            }

            // ── Mode cloud : interception ──
            var gpu = mode.split(':')[1];
            if (!gpu) {
                showNotification('⚠️  GPU invalide, fallback en local.', 'error');
                return originalQueue(number, workflow, options);
            }

            // Stocker le GPU courant pour les appels d'upload direct
            currentGpu = gpu.toUpperCase();

            try {
                // Étape 1 : encoder / uploader les images locales
                showLoading('☁️  Transfert des fichiers vers ' + currentGpu + '...');
                var enrichedWorkflow = await encodeLocalImages(workflow);

                // In new ComfyUI, queuePrompt receives { output: <api_format>, workflow: <ui_format> }
                // We need the API format (output key) to send to the remote ComfyUI
                if (enrichedWorkflow && enrichedWorkflow.output && typeof enrichedWorkflow.output === 'object') {
                    console.log('Modal Gateway: detected new ComfyUI format, extracting output key');
                    enrichedWorkflow = enrichedWorkflow.output;
                }

                console.log('Modal Gateway: workflow keys =', Object.keys(enrichedWorkflow));
                console.log('Modal Gateway: workflow has SaveImage?', JSON.stringify(enrichedWorkflow).includes('SaveImage'));
                console.log('Modal Gateway: workflow sample =', JSON.stringify(enrichedWorkflow).substring(0, 500));

                // Étape 2 : envoyer le workflow à Modal
                showLoading('☁️  Génération sur ' + currentGpu + ' en cours...\nPatiente un instant, le temps que le worker démarre.');

                var controller = new AbortController();
                var timeoutId = setTimeout(function () {
                    controller.abort();
                }, CONFIG.TIMEOUT_MS);

                var response = await fetch(CONFIG.API_URL + '/generate', {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json',
                        'X-API-Key': CONFIG.API_KEY,
                    },
                    body: JSON.stringify({
                        workflow: enrichedWorkflow,
                        gpu: currentGpu,
                    }),
                    signal: controller.signal,
                });

                clearTimeout(timeoutId);

                if (!response.ok) {
                    var errorText = '';
                    try {
                        errorText = await response.text();
                    } catch (_) {
                        errorText = '(erreur de lecture)';
                    }
                    throw new Error('HTTP ' + response.status + ': ' + errorText);
                }

                var result = await response.json();
                hideLoading();

                // Afficher les images générées dans le canvas ComfyUI
                if (result.images && result.images.length > 0) {
                    for (var i = 0; i < result.images.length; i++) {
                        var img = result.images[i];
                        if (img.data) {
                            displayImageInCanvas(img.data, img.filename || 'output_' + i + '.png', i === result.images.length - 1);
                        }
                    }
                    showNotification('✅  ' + result.images.length + ' image(s) reçue(s) depuis ' + gpu.toUpperCase(), 'success');
                } else {
                    showNotification('✅  Génération terminée sur ' + gpu.toUpperCase() + ' (aucune image retournée)', 'info');
                }

                return result;

            } catch (error) {
                hideLoading();

                if (error.name === 'AbortError') {
                    console.error('Modal Gateway: Timeout après', CONFIG.TIMEOUT_MS, 'ms');
                    showNotification('⏱️  Timeout — Modal ne répond pas après ' + (CONFIG.TIMEOUT_MS / 1000) + 's. Passage en local.', 'error');
                } else {
                    console.error('Modal Gateway Error:', error);
                    showNotification(
                        '⚠️  Modal inaccessible (' +
                            (error.message ? error.message.substring(0, 80) : 'erreur inconnue') +
                            '). Passage en local.',
                        'error'
                    );
                }

                // Fallback : exécuter localement
                return originalQueue(number, workflow, options);
            }
        };

        return true; // Interception réussie
    }

    // ─── Affichage des images reçues dans le canvas ─────────────────────────

    /**
     * Affiche les images générées (base64) dans un overlay plein écran sur la page.
     *
     * Au lieu d'utiliser window.open() avec une data URL — ce qui est bloqué par
     * les popup blockers — on crée un overlay modal dans la page courante.
     * Les images sont collectées au fur et à mesure et affichées quand la dernière
     * arrive (isLast = true).
     *
     * @param {string} base64Data - Données de l'image encodées en base64 (sans préfixe).
     * @param {string} filename - Nom du fichier (pour référence / téléchargement).
     * @param {boolean} [isLast=false] - Si true, affiche l'overlay avec toutes les images collectées.
     */
    function displayImageInCanvas(base64Data, filename, isLast) {
        // Store images and show overlay when the last one arrives
        if (!window._modalGatewayImages) {
            window._modalGatewayImages = [];
        }
        window._modalGatewayImages.push({
            data: base64Data,
            filename: filename || 'modal_output.png',
        });

        if (!isLast) return;

        // Show the overlay with all collected images
        var images = window._modalGatewayImages;
        window._modalGatewayImages = []; // reset for next batch

        // Remove any existing overlay
        var existing = document.getElementById('modal-image-overlay');
        if (existing) existing.remove();

        var overlay = document.createElement('div');
        overlay.id = 'modal-image-overlay';
        overlay.style.cssText = [
            'position: fixed', 'top: 0', 'left: 0', 'width: 100%', 'height: 100%',
            'background: rgba(0,0,0,0.85)', 'z-index: 99999',
            'display: flex', 'flex-direction: column', 'align-items: center', 'justify-content: center',
            'padding: 20px', 'box-sizing: border-box', 'backdrop-filter: blur(4px)',
        ].join(';') + ';';

        // Header bar
        var header = document.createElement('div');
        header.style.cssText = [
            'position: absolute', 'top: 0', 'left: 0', 'right: 0',
            'display: flex', 'justify-content: space-between', 'align-items: center',
            'padding: 16px 24px', 'box-sizing: border-box',
        ].join(';') + ';';

        var title = document.createElement('div');
        title.style.cssText = 'color: #fff; font-size: 16px; font-family: sans-serif;';
        title.textContent = '🖼️ Modal Gateway — ' + images.length + ' image(s) reçue(s)';

        var closeBtn = document.createElement('button');
        closeBtn.innerHTML = '✕';
        closeBtn.style.cssText = [
            'background: rgba(255,255,255,0.15)', 'border: none', 'color: #fff',
            'font-size: 24px', 'cursor: pointer', 'padding: 4px 12px',
            'border-radius: 6px', 'line-height: 1',
        ].join(';') + ';';
        closeBtn.onmouseover = function () { closeBtn.style.background = 'rgba(255,255,255,0.3)'; };
        closeBtn.onmouseout = function () { closeBtn.style.background = 'rgba(255,255,255,0.15)'; };

        header.appendChild(title);
        header.appendChild(closeBtn);
        overlay.appendChild(header);

        // Image container (scrollable if multiple images)
        var imgContainer = document.createElement('div');
        imgContainer.style.cssText = [
            'max-width: 90%', 'max-height: 85%', 'overflow-y: auto',
            'display: flex', 'flex-direction: ' + (images.length > 1 ? 'column' : 'row'),
            'gap: 16px', 'align-items: ' + (images.length > 1 ? 'center' : 'stretch'),
            'padding-top: 60px',
        ].join(';') + ';';

        for (var i = 0; i < images.length; i++) {
            (function (imgData, imgFilename) {
                var wrapper = document.createElement('div');
                wrapper.style.cssText = 'position: relative; display: inline-block;';

                var img = document.createElement('img');
                img.src = 'data:image/png;base64,' + imgData.data;
                img.style.cssText = [
                    'max-width: 100%', 'max-height: 80vh',
                    'border-radius: 8px', 'box-shadow: 0 8px 32px rgba(0,0,0,0.5)',
                    'object-fit: contain',
                ].join(';') + ';';

                // Download button (always visible)
                var dlBtn = document.createElement('a');
                dlBtn.href = 'data:image/png;base64,' + imgData.data;
                dlBtn.download = imgFilename;
                dlBtn.innerHTML = '💾 Save';
                dlBtn.style.cssText = [
                    'position: absolute', 'top: 12px', 'right: 12px',
                    'background: rgba(74,74,255,0.9)', 'color: #fff',
                    'padding: 8px 16px', 'border-radius: 8px',
                    'font-size: 14px', 'font-family: sans-serif',
                    'text-decoration: none', 'cursor: pointer',
                    'font-weight: 600', 'box-shadow: 0 4px 12px rgba(0,0,0,0.4)',
                    'transition: background 0.2s',
                ].join(';') + ';';
                dlBtn.onmouseover = function () { dlBtn.style.background = 'rgba(90,90,255,1)'; };
                dlBtn.onmouseout = function () { dlBtn.style.background = 'rgba(74,74,255,0.9)'; };

                wrapper.appendChild(img);
                wrapper.appendChild(dlBtn);
                imgContainer.appendChild(wrapper);
            })(images[i], images[i].filename);
        }

        overlay.appendChild(imgContainer);
        document.body.appendChild(overlay);

        // Close handlers
        function closeOverlay() {
            if (overlay.parentNode) overlay.parentNode.removeChild(overlay);
            document.removeEventListener('keydown', escHandler);
        }

        function escHandler(e) {
            if (e.key === 'Escape') closeOverlay();
        }

        closeBtn.onclick = closeOverlay;
        overlay.onclick = function (e) {
            if (e.target === overlay || e.target === header) closeOverlay();
        };
        document.addEventListener('keydown', escHandler);

        console.log('Modal Gateway: ' + images.length + ' image(s) affichée(s) dans l\'overlay');
    }

    // ─── Indicateur de chargement ───────────────────────────────────────────

    /**
     * Affiche un overlay semi-transparent avec un message de chargement.
     * @param {string} msg - Message à afficher.
     */
    function showLoading(msg) {
        var existing = document.getElementById('modal-loading-overlay');
        if (existing) {
            var content = document.getElementById('modal-loading-content');
            if (content) content.textContent = msg;
            return;
        }

        var overlay = document.createElement('div');
        overlay.id = 'modal-loading-overlay';

        var inner = document.createElement('div');
        inner.id = 'modal-loading-content';
        inner.textContent = msg;

        overlay.appendChild(inner);
        document.body.appendChild(overlay);
    }

    /**
     * Supprime l'overlay de chargement.
     */
    function hideLoading() {
        var el = document.getElementById('modal-loading-overlay');
        if (el) el.remove();
    }

    // ─── Notifications utilisateur ──────────────────────────────────────────

    /**
     * Affiche une notification temporaire en bas à droite.
     * @param {string} msg - Message à afficher.
     * @param {'error'|'success'|'info'} type - Type de notification (couleur).
     */
    function showNotification(msg, type) {
        type = type || 'info';

        var el = document.createElement('div');
        el.className = 'modal-notification modal-notification--' + type;
        el.textContent = msg;

        document.body.appendChild(el);

        // Disparition automatique après 5 secondes
        setTimeout(function () {
            el.style.opacity = '0';
            el.style.transition = 'opacity 0.5s ease';
            setTimeout(function () {
                if (el.parentNode) el.parentNode.removeChild(el);
            }, 500);
        }, 5000);
    }

    // ─── Styles CSS de la modale ────────────────────────────────────────────

    /**
     * Injecte les styles CSS pour la modale de configuration dans le <head>.
     * Appelée une fois au démarrage.
     */
    function injectModalStyles() {
        if (document.getElementById('modal-settings-styles')) return;

        var style = document.createElement('style');
        style.id = 'modal-settings-styles';
        style.textContent = [
            // ─── Drodown du sélecteur GPU (ex-web/modal_gateway.css) ───
            '#modal-gateway-container {',
            '  display: inline-flex;',
            '  align-items: center;',
            '  margin: 0 8px;',
            '  gap: 4px;',
            '  user-select: none;',
            '}',
            '#modal-gpu-selector {',
            '  background: #2a2a2a;',
            '  color: #e0e0e0;',
            '  border: 1px solid #555;',
            '  border-radius: 4px;',
            '  padding: 4px 8px;',
            '  font-size: 13px;',
            '  font-family: sans-serif;',
            '  cursor: pointer;',
            '  outline: none;',
            '  transition: border-color 0.2s ease, box-shadow 0.2s ease;',
            '}',
            '#modal-gpu-selector:hover { border-color: #888; }',
            '#modal-gpu-selector:focus {',
            '  border-color: #4caf50;',
            '  box-shadow: 0 0 0 2px rgba(76, 175, 80, 0.25);',
            '}',
            '#modal-gpu-selector option {',
            '  background: #1e1e1e;',
            '  color: #e0e0e0;',
            '  padding: 4px 8px;',
            '}',
            '',
            // ─── Overlay de chargement ───
            '#modal-loading-overlay {',
            '  position: fixed;',
            '  top: 0;',
            '  left: 0;',
            '  width: 100%;',
            '  height: 100%;',
            '  background: rgba(0, 0, 0, 0.65);',
            '  display: flex;',
            '  align-items: center;',
            '  justify-content: center;',
            '  z-index: 99999;',
            '  backdrop-filter: blur(2px);',
            '}',
            '#modal-loading-content {',
            '  background: #1a1a2e;',
            '  color: #fff;',
            '  padding: 24px 36px;',
            '  border-radius: 12px;',
            '  font-size: 18px;',
            '  font-family: sans-serif;',
            '  box-shadow: 0 8px 32px rgba(0, 0, 0, 0.5);',
            '  border: 1px solid rgba(255, 255, 255, 0.1);',
            '  max-width: 80vw;',
            '  text-align: center;',
            '  line-height: 1.5;',
            '}',
            '#modal-loading-content::before {',
            '  content: "";',
            '  display: inline-block;',
            '  width: 20px;',
            '  height: 20px;',
            '  margin-right: 12px;',
            '  border: 3px solid rgba(255, 255, 255, 0.2);',
            '  border-top-color: #4caf50;',
            '  border-radius: 50%;',
            '  animation: modal-spin 0.8s linear infinite;',
            '  vertical-align: middle;',
            '}',
            '@keyframes modal-spin { to { transform: rotate(360deg); } }',
            '',
            // ─── Notification ───
            '.modal-notification {',
            '  position: fixed;',
            '  bottom: 20px;',
            '  right: 20px;',
            '  padding: 12px 20px;',
            '  border-radius: 8px;',
            '  font-size: 14px;',
            '  z-index: 99998;',
            '  font-family: sans-serif;',
            '  animation: modal-fadeIn 0.3s ease-out;',
            '  color: #fff;',
            '  box-shadow: 0 4px 16px rgba(0, 0, 0, 0.35);',
            '  max-width: 360px;',
            '  line-height: 1.4;',
            '}',
            '.modal-notification--error {',
            '  background: #c0392b;',
            '  border-left: 4px solid #e74c3c;',
            '}',
            '.modal-notification--success {',
            '  background: #27ae60;',
            '  border-left: 4px solid #2ecc71;',
            '}',
            '.modal-notification--info {',
            '  background: #2980b9;',
            '  border-left: 4px solid #3498db;',
            '}',
            '@keyframes modal-fadeIn {',
            '  from { opacity: 0; transform: translateY(12px); }',
            '  to { opacity: 1; transform: translateY(0); }',
            '}',
            '',
            // ─── Overlay de la modale de configuration ───
            '#modal-settings-overlay {',
            '  position: fixed; top: 0; left: 0; width: 100%; height: 100%;',
            '  background: rgba(0,0,0,0.6); z-index: 9999;',
            '  display: flex; align-items: center; justify-content: center;',
            '  animation: modal-fade-in 0.2s ease;',
            '}',
            '@keyframes modal-fade-in {',
            '  from { opacity: 0; }',
            '  to { opacity: 1; }',
            '}',
            '/* Panneau de la modale */',
            '.modal-settings-panel {',
            '  background: #2a2a2e; color: #e0e0e0;',
            '  border-radius: 12px; width: 560px; max-width: 90vw;',
            '  max-height: 85vh; overflow-y: auto;',
            '  box-shadow: 0 8px 32px rgba(0,0,0,0.5);',
            '  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;',
            '  font-size: 14px; line-height: 1.5;',
            '  animation: modal-slide-in 0.25s ease;',
            '}',
            '@keyframes modal-slide-in {',
            '  from { transform: translateY(-20px); opacity: 0; }',
            '  to { transform: translateY(0); opacity: 1; }',
            '}',
            '/* En-tête */',
            '.modal-settings-header {',
            '  display: flex; justify-content: space-between; align-items: center;',
            '  padding: 16px 20px; border-bottom: 1px solid #3a3a3e;',
            '}',
            '.modal-settings-header h2 {',
            '  margin: 0; font-size: 16px; font-weight: 600;',
            '}',
            '.modal-settings-close {',
            '  background: none; border: none; color: #888;',
            '  cursor: pointer; font-size: 20px; padding: 0 4px;',
            '  line-height: 1;',
            '}',
            '.modal-settings-close:hover { color: #fff; }',
            '/* Corps */',
            '.modal-settings-body { padding: 20px; }',
            '.modal-settings-body section {',
            '  margin-bottom: 20px; padding-bottom: 20px;',
            '  border-bottom: 1px solid #3a3a3e;',
            '}',
            '.modal-settings-body section:last-child {',
            '  margin-bottom: 0; padding-bottom: 0;',
            '  border-bottom: none;',
            '}',
            '.modal-settings-body h3 {',
            '  margin: 0 0 12px 0; font-size: 14px; font-weight: 600; color: #aaa;',
            '  text-transform: uppercase; letter-spacing: 0.5px;',
            '}',
            '/* Champs de formulaire */',
            '.modal-settings-body label {',
            '  display: block; margin: 8px 0 4px;',
            '  font-size: 12px; color: #999;',
            '}',
            '.modal-settings-body input[type="text"],',
            '.modal-settings-body input[type="password"] {',
            '  width: 100%; padding: 8px 10px; margin-bottom: 4px;',
            '  border: 1px solid #444; border-radius: 6px;',
            '  background: #1a1a1e; color: #e0e0e0;',
            '  font-size: 13px; box-sizing: border-box;',
            '  outline: none;',
            '}',
            '.modal-settings-body input:focus {',
            '  border-color: #6a6aff;',
            '}',
            '/* Boutons */',
            '.modal-btn {',
            '  display: inline-block; padding: 8px 16px; margin-top: 8px;',
            '  border: none; border-radius: 6px;',
            '  cursor: pointer; font-size: 13px; font-weight: 500;',
            '  transition: background 0.2s;',
            '}',
            '.modal-btn:disabled {',
            '  opacity: 0.6; cursor: not-allowed;',
            '}',
            '.modal-btn-primary {',
            '  background: #4a4aff; color: #fff;',
            '}',
            '.modal-btn-primary:hover:not(:disabled) {',
            '  background: #5a5aff;',
            '}',
            '.modal-btn-action {',
            '  background: #2d7d46; color: #fff;',
            '}',
            '.modal-btn-action:hover:not(:disabled) {',
            '  background: #3a9d56;',
            '}',
            '/* Statuts */',
            '.modal-status-row {',
            '  display: flex; justify-content: space-between; align-items: center;',
            '  padding: 8px 0;',
            '}',
            '.status-badge {',
            '  font-size: 12px; color: #aaa;',
            '}',
            '.modal-status-grid {',
            '  display: grid; grid-template-columns: 1fr 1fr; gap: 8px;',
            '}',
            '.status-item {',
            '  padding: 8px 12px; background: #1a1a1e;',
            '  border-radius: 6px; font-size: 13px;',
            '}',
            '/* Logs */',
            '.modal-log-output {',
            '  background: #111; color: #0f0;',
            '  padding: 12px; border-radius: 6px;',
            '  font-family: "Courier New", monospace;',
            '  font-size: 12px; line-height: 1.4;',
            '  max-height: 300px; overflow-y: auto;',
            '  white-space: pre-wrap; word-break: break-all;',
            '}',
            '/* Scrollbar */',
            '.modal-settings-panel::-webkit-scrollbar,',
            '.modal-log-output::-webkit-scrollbar {',
            '  width: 6px;',
            '}',
            '.modal-settings-panel::-webkit-scrollbar-track,',
            '.modal-log-output::-webkit-scrollbar-track {',
            '  background: transparent;',
            '}',
            '.modal-settings-panel::-webkit-scrollbar-thumb,',
            '.modal-log-output::-webkit-scrollbar-thumb {',
            '  background: #555; border-radius: 3px;',
            '}',
            '',
            // ─── Plugins detection section ───
            '.modal-plugins-list {',
            '  max-height: 280px; overflow-y: auto;',
            '  border: 1px solid #3a3a3e; border-radius: 8px;',
            '  padding: 8px; margin-top: 8px; background: #1a1a1e;',
            '}',
            '.modal-plugin-item {',
            '  display: flex; align-items: flex-start; gap: 8px;',
            '  padding: 8px 6px; border-bottom: 1px solid #2e2e32;',
            '  cursor: pointer; transition: background 0.15s;',
            '}',
            '.modal-plugin-item:last-child { border-bottom: none; }',
            '.modal-plugin-item:hover { background: #252530; }',
            '.modal-plugin-item input[type="checkbox"] {',
            '  margin-top: 3px; flex-shrink: 0; cursor: pointer;',
            '  width: 16px; height: 16px; accent-color: #4a4aff;',
            '}',
            '.modal-plugin-item-content { flex: 1; min-width: 0; }',
            '.modal-plugin-item-name {',
            '  font-size: 13px; font-weight: 600; color: #e0e0e0;',
            '}',
            '.modal-plugin-item-url {',
            '  font-size: 11px; color: #777; word-break: break-all;',
            '  margin-top: 2px; font-family: "Courier New", monospace;',
            '}',
            '.modal-plugin-item-nogit {',
            '  font-size: 11px; color: #c0a030; margin-top: 2px;',
            '}',
            '.modal-plugin-actions {',
            '  display: flex; gap: 8px; margin-top: 12px; flex-wrap: wrap;',
            '}',
            '.modal-plugin-note {',
            '  font-size: 11px; color: #888; margin-top: 10px;',
            '  padding: 8px 10px; background: #1e2a1e; border-radius: 6px;',
            '  border-left: 3px solid #4a8a4a;',
            '}',
            '.modal-plugin-loading {',
            '  font-size: 13px; color: #aaa; text-align: center;',
            '  padding: 24px;',
            '}',
            '.modal-plugin-empty {',
            '  font-size: 13px; color: #777; text-align: center;',
            '  padding: 20px; font-style: italic;',
            '}',
            '',
            // ─── Models detection section (reuses plugin styles) ───
            '.modal-models-list {',
            '  max-height: 280px; overflow-y: auto;',
            '  border: 1px solid #3a3a3e; border-radius: 8px;',
            '  padding: 8px; margin-top: 8px; background: #1a1a1e;',
            '}',
            '.modal-model-item {',
            '  display: flex; align-items: flex-start; gap: 8px;',
            '  padding: 8px 6px; border-bottom: 1px solid #2e2e32;',
            '  cursor: pointer; transition: background 0.15s;',
            '}',
            '.modal-model-item:last-child { border-bottom: none; }',
            '.modal-model-item:hover { background: #252530; }',
            '.modal-model-item input[type="checkbox"] {',
            '  margin-top: 3px; flex-shrink: 0; cursor: pointer;',
            '  width: 16px; height: 16px; accent-color: #4a8aff;',
            '}',
            '.modal-model-item-content { flex: 1; min-width: 0; }',
            '.modal-model-item-name {',
            '  font-size: 13px; font-weight: 600; color: #e0e0e0;',
            '  word-break: break-all;',
            '}',
            '.modal-model-item-info {',
            '  font-size: 11px; color: #777; margin-top: 2px;',
            '}',
        ].join('\n');
        document.head.appendChild(style);
        console.log('Modal Gateway: styles injectés');
    }

    // ─── Fonctions utilitaires ───────────────────────────────────────────────

    /**
     * Échappe les caractères HTML pour éviter les injections XSS.
     * @param {string} str - Chaîne à échapper.
     * @returns {string}
     */
    function escapeHtml(str) {
        var div = document.createElement('div');
        div.textContent = str;
        return div.innerHTML;
    }

    /**
     * Rafraîchit les indicateurs de statut dans la modale.
     */
    function refreshStatus() {
        fetch('/api/modal/status')
            .then(function (r) { return r.json(); })
            .then(function (status) {
                var items = document.querySelectorAll('.status-item');
                if (items.length >= 4) {
                    items[0].textContent = '🔧 Modal CLI : ' + (status.modal_installed ? '✅' : '❌');
                    items[1].textContent = '🔐 Authentifié : ' + (status.modal_authenticated ? '✅' : '❌');
                    items[2].textContent = '💾 Volume comfy-models : ' + (status.volume_exists ? '✅' : '❌');
                    items[3].textContent = '🌐 API configurée : ' + (status.api_configured ? '✅' : '❌');
                }
            })
            .catch(function () {});
    }

    /**
     * Lance une opération (sync/deploy) et affiche les logs en direct via SSE.
     * @param {'sync'|'deploy'} operation
     */
    async function runOperation(operation) {
        var btn = document.getElementById('cfg-run-' + operation);
        var logSection = document.getElementById('modal-log-section');
        var logOutput = document.getElementById('modal-log-output');

        if (!btn || !logSection || !logOutput) return;

        btn.disabled = true;
        btn.textContent = '⏳ En cours...';
        logSection.style.display = 'block';
        logOutput.textContent = '';

        try {
            // Lancer l'opération
            var opResp = await fetch('/api/modal/' + operation, { method: 'POST' });
            if (!opResp.ok) {
                var errText = '';
                try { errText = await opResp.text(); } catch (_) {}
                throw new Error('HTTP ' + opResp.status + ' ' + errText);
            }

            // Lire les logs en SSE
            var eventSource = new EventSource('/api/modal/logs/stream');

            eventSource.onmessage = function (event) {
                try {
                    var data = JSON.parse(event.data);
                    if (data.line) {
                        logOutput.textContent += data.line + '\n';
                        logOutput.scrollTop = logOutput.scrollHeight;
                    }
                } catch (e) {}
            };

            eventSource.addEventListener('done', function (event) {
                eventSource.close();
                var data;
                try {
                    data = JSON.parse(event.data);
                } catch (e) {
                    data = { code: '1' };
                }
                btn.disabled = false;
                btn.textContent = data.code === '0' ? '✅ Terminé' : '❌ Échec';
                if (data.code === '0') {
                    showNotification(
                        '✅ ' + (operation === 'sync' ? 'Sync' : 'Déploiement') + ' réussi !',
                        'success'
                    );
                    // Rafraîchir le statut
                    setTimeout(refreshStatus, 1000);
                } else {
                    showNotification(
                        '❌ ' + (operation === 'sync' ? 'Sync' : 'Déploiement') + ' échoué — voir logs',
                        'error'
                    );
                }
            });

            eventSource.onerror = function () {
                eventSource.close();
                btn.disabled = false;
                btn.textContent = '❌ Erreur connexion';
                logOutput.textContent += '\n⚠️ Connexion SSE perdue.\n';
            };

        } catch (e) {
            btn.disabled = false;
            btn.textContent = '❌ Erreur';
            logOutput.textContent = e.message || 'Erreur inconnue';
        }
    }

    /**
     * Ouvre la modale de configuration Modal Gateway.
     * Charge la config et le statut depuis l'API ComfyUI.
     */
    async function openSettingsModal() {
        // Empêcher l'ouverture de multiples modales
        if (document.getElementById('modal-settings-overlay')) return;

        // Charger la config depuis l'API ComfyUI
        var config = { api_url: CONFIG.API_URL, api_key: CONFIG.API_KEY };
        try {
            var resp = await fetch('/api/modal/config');
            if (resp.ok) config = await resp.json();
        } catch (e) { /* mode dégradé : utiliser les constantes */ }

        // Charger le statut
        var status = {};
        try {
            var statusResp = await fetch('/api/modal/status');
            if (statusResp.ok) status = await statusResp.json();
        } catch (e) {}

        // Créer l'overlay
        var overlay = document.createElement('div');
        overlay.id = 'modal-settings-overlay';
        overlay.innerHTML = [
            '<div class="modal-settings-panel">',
            '  <div class="modal-settings-header">',
            '    <h2>⚙️ Modal Gateway — Configuration</h2>',
            '    <button class="modal-settings-close">✕</button>',
            '  </div>',
            '  <div class="modal-settings-body">',
            '    <section>',
            '      <h3>🔌 Connexion API Modal</h3>',
            '      <label>URL de l\'API</label>',
            '      <input type="text" id="cfg-api-url" value="' + escapeHtml(config.api_url || '') + '" placeholder="https://xxx.modal.run" />',
            '      <label>Clé API (X-API-Key)</label>',
            '      <input type="password" id="cfg-api-key" value="' + escapeHtml(config.api_key || '') + '" placeholder="Votre clé secrète" />',
            '      <button id="cfg-save-connection" class="modal-btn modal-btn-primary">💾 Sauvegarder</button>',
            '    </section>',
            '    <section>',
            '      <h3>📦 Modèles</h3>',
            '      <p style="font-size:12px;color:#999;margin:0 0 8px 0;">Détectez vos modèles locaux et synchronisez-les vers Modal.</p>',
            '      <div class="modal-plugin-actions">',
            '        <button id="cfg-detect-models" class="modal-btn modal-btn-action">🔄 Détecter mes modèles</button>',
            '        <button id="cfg-save-models" class="modal-btn modal-btn-primary" disabled>💾 Save Selection</button>',
            '      </div>',
            '      <div id="modal-models-list" class="modal-models-list" style="display:none;">',
            '      </div>',
            '      <div id="modal-models-loading" class="modal-plugin-loading" style="display:none;">⏳ Scan en cours...</div>',
            '      <div id="modal-models-empty" class="modal-plugin-empty" style="display:none;">Aucun modèle détecté.</div>',
            '      <div class="modal-status-row" style="margin-top:12px;">',
            '        <span>🔄 Synchronisation des modèles</span>',
            '        <span class="status-badge" id="status-sync">' + (config.last_sync ? '✅ ' + escapeHtml(config.last_sync) : '⏳ Jamais') + '</span>',
            '      </div>',
            '      <button id="cfg-run-sync" class="modal-btn modal-btn-action">📥 Sync Models</button>',
            '      <div class="modal-plugin-note">ℹ️ Sauvegardez votre sélection puis cliquez sur <strong>Sync Models</strong> pour uploader vers Modal.</div>',
            '    </section>',
            '    <section>',
            '      <h3>🚀 Déploiement</h3>',
            '      <div class="modal-status-row">',
            '        <span>🌐 API Gateway déployée</span>',
            '        <span class="status-badge" id="status-deploy">' + (config.last_deploy ? '✅ ' + escapeHtml(config.last_deploy) : '⏳ Jamais') + '</span>',
            '      </div>',
            '      <button id="cfg-run-deploy" class="modal-btn modal-btn-action">🚀 Deploy API</button>',
            '    </section>',
            '    <section>',
            '      <h3>📊 Statut Modal</h3>',
            '      <div class="modal-status-grid">',
            '        <div class="status-item">🔧 Modal CLI : ' + (status.modal_installed ? '✅' : '❌') + '</div>',
            '        <div class="status-item">🔐 Authentifié : ' + (status.modal_authenticated ? '✅' : '❌') + '</div>',
            '        <div class="status-item">💾 Volume comfy-models : ' + (status.volume_exists ? '✅' : '❌') + '</div>',
            '        <div class="status-item">🌐 API configurée : ' + (status.api_configured ? '✅' : '❌') + '</div>',
            '      </div>',
            '    </section>',
            '    <section>',
            '      <h3>🧩 Custom Nodes</h3>',
            '      <p style="font-size:12px;color:#999;margin:0 0 8px 0;">Détectez vos custom nodes locaux et synchronisez-les vers Modal.</p>',
            '      <div class="modal-plugin-actions">',
            '        <button id="cfg-detect-plugins" class="modal-btn modal-btn-action">🔄 Détecter mes nodes</button>',
            '        <button id="cfg-save-plugins" class="modal-btn modal-btn-primary" disabled>💾 Save Plugins</button>',
            '      </div>',
            '      <div id="modal-plugins-list" class="modal-plugins-list" style="display:none;">',
            '      </div>',
            '      <div id="modal-plugins-loading" class="modal-plugin-loading" style="display:none;">⏳ Scan en cours...</div>',
            '      <div id="modal-plugins-empty" class="modal-plugin-empty" style="display:none;">Aucun node détecté.</div>',
            '      <div class="modal-plugin-note">ℹ️ Les changements nécessitent un <strong>redeploy</strong> (bouton 🚀 Deploy API) pour prendre effet sur Modal.</div>',
            '    </section>',
            '    <section id="modal-log-section" style="display:none;">',
            '      <h3>📋 Logs</h3>',
            '      <pre id="modal-log-output" class="modal-log-output"></pre>',
            '    </section>',
            '  </div>',
            '</div>',
        ].join('\n');
        document.body.appendChild(overlay);

        // Gestionnaires d'événements
        overlay.querySelector('.modal-settings-close').onclick = function () { overlay.remove(); };
        overlay.onclick = function (e) { if (e.target === overlay) overlay.remove(); };

        document.getElementById('cfg-save-connection').onclick = async function () {
            var apiUrl = document.getElementById('cfg-api-url').value.trim();
            var apiKey = document.getElementById('cfg-api-key').value.trim();
            try {
                var saveResp = await fetch('/api/modal/config', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ api_url: apiUrl, api_key: apiKey }),
                });
                if (!saveResp.ok) {
                    throw new Error('HTTP ' + saveResp.status);
                }
                // Mettre à jour les constantes
                CONFIG.API_URL = apiUrl;
                CONFIG.API_KEY = apiKey;
                // Persister en localStorage
                if (apiUrl) localStorage.setItem('modal-api-url', apiUrl);
                if (apiKey) localStorage.setItem('modal-api-key', apiKey);
                showNotification('✅ Configuration sauvegardée', 'success');
            } catch (e) {
                showNotification('❌ Erreur lors de la sauvegarde : ' + e.message, 'error');
            }
        };

        document.getElementById('cfg-run-sync').onclick = function () { runOperation('sync'); };
        document.getElementById('cfg-run-deploy').onclick = function () { runOperation('deploy'); };

        var detectedPlugins = [];
        var savedPlugins = null;

        document.getElementById('cfg-detect-plugins').onclick = async function () {
            var btn = document.getElementById('cfg-detect-plugins');
            var listEl = document.getElementById('modal-plugins-list');
            var loadingEl = document.getElementById('modal-plugins-loading');
            var emptyEl = document.getElementById('modal-plugins-empty');
            var saveBtn = document.getElementById('cfg-save-plugins');

            btn.disabled = true;
            btn.textContent = '⏳ Scan...';
            listEl.style.display = 'none';
            emptyEl.style.display = 'none';
            loadingEl.style.display = 'block';

            try {
                if (savedPlugins === null) {
                    try {
                        var savedResp = await fetch('/api/modal/plugins');
                        if (savedResp.ok) {
                            savedPlugins = await savedResp.json();
                        } else {
                            savedPlugins = { custom_nodes: [], custom_nodes_ext: [] };
                        }
                    } catch (e) {
                        savedPlugins = { custom_nodes: [], custom_nodes_ext: [] };
                    }
                }

                var resp = await fetch('/api/modal/plugins/detect');
                if (!resp.ok) throw new Error('HTTP ' + resp.status);
                var data = await resp.json();
                detectedPlugins = data.plugins || [];

                loadingEl.style.display = 'none';
                btn.disabled = false;
                btn.textContent = '🔄 Détecter mes nodes';

                if (detectedPlugins.length === 0) {
                    emptyEl.style.display = 'block';
                    saveBtn.disabled = true;
                    return;
                }

                var savedExtUrls = {};
                if (savedPlugins && savedPlugins.custom_nodes_ext) {
                    for (var s = 0; s < savedPlugins.custom_nodes_ext.length; s++) {
                        var p = savedPlugins.custom_nodes_ext[s];
                        if (p.url) savedExtUrls[p.url] = p;
                    }
                }

                listEl.innerHTML = '';
                for (var i = 0; i < detectedPlugins.length; i++) {
                    (function (plugin) {
                        var item = document.createElement('label');
                        item.className = 'modal-plugin-item';

                        var checkbox = document.createElement('input');
                        checkbox.type = 'checkbox';
                        checkbox.dataset.pluginName = plugin.name;
                        checkbox.dataset.pluginUrl = plugin.git_url || '';
                        if (plugin.git_url && savedExtUrls[plugin.git_url]) {
                            checkbox.checked = true;
                        }

                        var content = document.createElement('div');
                        content.className = 'modal-plugin-item-content';

                        var nameEl = document.createElement('div');
                        nameEl.className = 'modal-plugin-item-name';
                        nameEl.textContent = plugin.name;
                        content.appendChild(nameEl);

                        if (plugin.has_git && plugin.git_url) {
                            var urlEl = document.createElement('div');
                            urlEl.className = 'modal-plugin-item-url';
                            urlEl.textContent = plugin.git_url;
                            content.appendChild(urlEl);
                        } else {
                            var noGitEl = document.createElement('div');
                            noGitEl.className = 'modal-plugin-item-nogit';
                            noGitEl.textContent = '⚠️ Pas de dépôt git — ne peut pas être synchronisé automatiquement';
                            content.appendChild(noGitEl);
                            checkbox.disabled = true;
                            checkbox.style.opacity = '0.4';
                        }

                        item.appendChild(checkbox);
                        item.appendChild(content);
                        listEl.appendChild(item);
                    })(detectedPlugins[i]);
                }

                listEl.style.display = 'block';
                saveBtn.disabled = false;

            } catch (e) {
                loadingEl.style.display = 'none';
                btn.disabled = false;
                btn.textContent = '🔄 Détecter mes nodes';
                showNotification('❌ Erreur détection nodes : ' + e.message, 'error');
            }
        };

        document.getElementById('cfg-save-plugins').onclick = async function () {
            var saveBtn = document.getElementById('cfg-save-plugins');
            var listEl = document.getElementById('modal-plugins-list');
            saveBtn.disabled = true;
            saveBtn.textContent = '⏳ Sauvegarde...';

            try {
                var checkboxes = listEl.querySelectorAll('input[type="checkbox"]:not(:disabled)');
                var customNodesExt = [];

                for (var i = 0; i < checkboxes.length; i++) {
                    var cb = checkboxes[i];
                    if (cb.checked && cb.dataset.pluginUrl) {
                        var existing = null;
                        if (savedPlugins && savedPlugins.custom_nodes_ext) {
                            for (var j = 0; j < savedPlugins.custom_nodes_ext.length; j++) {
                                if (savedPlugins.custom_nodes_ext[j].url === cb.dataset.pluginUrl) {
                                    existing = savedPlugins.custom_nodes_ext[j];
                                    break;
                                }
                            }
                        }
                        if (existing) {
                            customNodesExt.push(existing);
                        } else {
                            customNodesExt.push({
                                url: cb.dataset.pluginUrl,
                            });
                        }
                    }
                }

                var customNodes = (savedPlugins && savedPlugins.custom_nodes) || [];

                var resp = await fetch('/api/modal/plugins', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        custom_nodes: customNodes,
                        custom_nodes_ext: customNodesExt,
                    }),
                });

                if (!resp.ok) throw new Error('HTTP ' + resp.status);

                savedPlugins = {
                    custom_nodes: customNodes,
                    custom_nodes_ext: customNodesExt,
                };

                saveBtn.textContent = '✅ Sauvegardé';
                showNotification(
                    '✅ ' + customNodesExt.length + ' node(s) sauvegardé(s). Redeploy nécessaire.',
                    'success'
                );
                setTimeout(function () {
                    saveBtn.disabled = false;
                    saveBtn.textContent = '💾 Save Plugins';
                }, 2000);

            } catch (e) {
                saveBtn.disabled = false;
                saveBtn.textContent = '💾 Save Plugins';
                showNotification('❌ Erreur sauvegarde : ' + e.message, 'error');
            }
        };

        // ─── Models: detect & save ───
        var detectedModels = [];
        var savedModelsSelection = null;

        document.getElementById('cfg-detect-models').onclick = async function () {
            var btn = document.getElementById('cfg-detect-models');
            var listEl = document.getElementById('modal-models-list');
            var loadingEl = document.getElementById('modal-models-loading');
            var emptyEl = document.getElementById('modal-models-empty');
            var saveBtn = document.getElementById('cfg-save-models');

            btn.disabled = true;
            btn.textContent = '⏳ Scan...';
            listEl.style.display = 'none';
            emptyEl.style.display = 'none';
            loadingEl.style.display = 'block';

            try {
                // Load saved selection from config
                if (savedModelsSelection === null) {
                    try {
                        var savedResp = await fetch('/api/modal/models/select');
                        if (savedResp.ok) {
                            savedModelsSelection = await savedResp.json();
                        } else {
                            savedModelsSelection = { models_to_sync: [] };
                        }
                    } catch (e) {
                        savedModelsSelection = { models_to_sync: [] };
                    }
                }

                // Detect local models
                var resp = await fetch('/api/modal/models/detect');
                if (!resp.ok) throw new Error('HTTP ' + resp.status);
                var data = await resp.json();
                detectedModels = data.models || [];

                loadingEl.style.display = 'none';
                btn.disabled = false;
                btn.textContent = '🔄 Détecter mes modèles';

                if (detectedModels.length === 0) {
                    emptyEl.style.display = 'block';
                    saveBtn.disabled = true;
                    return;
                }

                // Build set of already-saved model keys
                var savedKeys = {};
                if (savedModelsSelection && savedModelsSelection.models_to_sync) {
                    for (var s = 0; s < savedModelsSelection.models_to_sync.length; s++) {
                        var m = savedModelsSelection.models_to_sync[s];
                        savedKeys[m.filename + '|' + m.model_dir] = true;
                    }
                }

                // Render list
                listEl.innerHTML = '';
                for (var i = 0; i < detectedModels.length; i++) {
                    (function (model) {
                        var key = model.filename + '|' + model.model_dir;
                        var item = document.createElement('label');
                        item.className = 'modal-model-item';

                        var checkbox = document.createElement('input');
                        checkbox.type = 'checkbox';
                        checkbox.dataset.modelFilename = model.filename;
                        checkbox.dataset.modelDir = model.model_dir;
                        checkbox.checked = !!savedKeys[key];

                        var content = document.createElement('div');
                        content.className = 'modal-model-item-content';

                        var nameEl = document.createElement('div');
                        nameEl.className = 'modal-model-item-name';
                        nameEl.textContent = model.filename;

                        var infoEl = document.createElement('div');
                        infoEl.className = 'modal-model-item-info';
                        infoEl.textContent = '📁 ' + model.model_dir + '  ·  ' + model.size_mb + ' MB';

                        content.appendChild(nameEl);
                        content.appendChild(infoEl);

                        item.appendChild(checkbox);
                        item.appendChild(content);
                        listEl.appendChild(item);
                    })(detectedModels[i]);
                }

                listEl.style.display = 'block';
                saveBtn.disabled = false;

            } catch (e) {
                loadingEl.style.display = 'none';
                btn.disabled = false;
                btn.textContent = '🔄 Détecter mes modèles';
                showNotification('❌ Erreur détection modèles : ' + e.message, 'error');
            }
        };

        document.getElementById('cfg-save-models').onclick = async function () {
            var saveBtn = document.getElementById('cfg-save-models');
            var listEl = document.getElementById('modal-models-list');
            saveBtn.disabled = true;
            saveBtn.textContent = '⏳ Sauvegarde...';

            try {
                var checkboxes = listEl.querySelectorAll('input[type="checkbox"]');
                var modelsToSync = [];

                for (var i = 0; i < checkboxes.length; i++) {
                    var cb = checkboxes[i];
                    if (cb.checked) {
                        modelsToSync.push({
                            filename: cb.dataset.modelFilename,
                            model_dir: cb.dataset.modelDir,
                        });
                    }
                }

                var resp = await fetch('/api/modal/models/select', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        models_to_sync: modelsToSync,
                    }),
                });

                if (!resp.ok) throw new Error('HTTP ' + resp.status);

                savedModelsSelection = { models_to_sync: modelsToSync };

                var totalMB = 0;
                for (var j = 0; j < detectedModels.length; j++) {
                    for (var k = 0; k < modelsToSync.length; k++) {
                        if (detectedModels[j].filename === modelsToSync[k].filename &&
                            detectedModels[j].model_dir === modelsToSync[k].model_dir) {
                            totalMB += detectedModels[j].size_mb;
                        }
                    }
                }

                saveBtn.textContent = '✅ Sauvegardé';
                showNotification(
                    '✅ ' + modelsToSync.length + ' modèle(s) sélectionné(s) — ' + Math.round(totalMB) + ' MB. Cliquez sur Sync Models.',
                    'success'
                );
                setTimeout(function () {
                    saveBtn.disabled = false;
                    saveBtn.textContent = '💾 Save Selection';
                }, 2000);

            } catch (e) {
                saveBtn.disabled = false;
                saveBtn.textContent = '💾 Save Selection';
                showNotification('❌ Erreur sauvegarde : ' + e.message, 'error');
            }
        };
    }

    // ─── Initialisation via l'API d'extension ComfyUI ───────────────────────

    /**
     * Attend que l'application ComfyUI soit disponible, puis enregistre
     * une extension ComfyUI pour injecter le dropdown dans la barre d'outils.
     *
     * Technique identique à FR.IA :
     * 1. Poke window.app / window.comfyAPI?.app?.app
     * 2. Appelle app.registerExtension()
     * 3. Dans setup(), trouve le bouton Settings via app.menu.settingsGroup.element
     * 4. Insère le dropdown avant ce bouton
     */
    (function waitForComfyApp() {
        var app = window.app || (
            window.comfyAPI &&
            window.comfyAPI.app &&
            window.comfyAPI.app.app
        );
        if (!app) {
            setTimeout(waitForComfyApp, 100);
            return;
        }

        console.log('Modal Gateway: app ComfyUI trouvée, enregistrement de l\'extension.');

        app.registerExtension({
            name: EXTENSION_NAME,
            async setup() {
                console.log('Modal Gateway: setup() de l\'extension appelé.');

                // Injecter les styles CSS dès que possible
                injectModalStyles();

                // Injecter le dropdown dans la barre d'outils (via le bouton Settings)
                setTimeout(function () {
                    injectDropdownInToolbar(app);
                }, 50);
            },
        });
    })();

    /**
     * Point d'entrée principal (initialisation legacy).
     * Injecte les styles et tente d'intercepter queuePrompt.
     * L'injection du dropdown est maintenant gérée par l'extension ci-dessus.
     */
    function init() {
        console.log('Modal Gateway: extension chargée.');
        console.log('Modal Gateway: document.readyState =', document.readyState);

        // ── Injecter les styles CSS (dropdown + modale) — sécurisé, already() checké ──
        injectModalStyles();

        // ── Intercepter Queue Prompt ──
        // On réessaie périodiquement jusqu'à ce que window.api soit disponible.
        var attempts = 0;
        var retryTimer = setInterval(function () {
            attempts++;
            var success = interceptQueuePrompt();
            if (success) {
                clearInterval(retryTimer);
                console.log('Modal Gateway: interception de queuePrompt activée.');
            } else if (attempts >= CONFIG.MAX_RETRIES) {
                clearInterval(retryTimer);
                console.warn(
                    'Modal Gateway: impossible d\'intercepter queuePrompt après ' +
                    attempts + ' tentatives. Rechargez la page.'
                );
                showNotification(
                    '⚠️  Modal Gateway n\'a pas pu s\'activer. Rechargez la page.',
                    'error'
                );
            }
        }, CONFIG.RETRY_INTERVAL);

        // ── Diagnostic (console) ──
        console.debug('Modal Gateway: API via findApi():', findApi() ? '✅ trouvée' : '❌ absente');
        var diagnosticSources = [
            'window.api',
            'window.comfyAPI.api',
            'window.comfyAPI.app.app.api',
            'window.comfyapp.api'
        ];
        for (var d = 0; d < diagnosticSources.length; d++) {
            var parts = diagnosticSources[d].split('.');
            var val = window;
            for (var p = 0; p < parts.length; p++) {
                if (val && typeof val === 'object' && parts[p] in val) {
                    val = val[parts[p]];
                } else {
                    val = undefined;
                    break;
                }
            }
            console.debug('Modal Gateway: ' + diagnosticSources[d] + ' queuePrompt ?',
                val && typeof val.queuePrompt === 'function' ? '✅' : '❌');
        }
    }

    // ─── Démarrage selon l'état du document ─────────────────────────────────

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        // DOMContentLoaded déjà passé — on lance directement
        init();
    }
})();
