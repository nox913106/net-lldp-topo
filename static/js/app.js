/**
 * Main Application JavaScript
 * Handles API calls, UI interactions, and data management
 */

// i18n.js is loaded before this file, so window.i18n is there. The guard is
// only for the case where it did not load at all.
const t = key => (window.i18n ? window.i18n.t(key) : key);

class App {
    constructor() {
        this.apiBase = '/api/v1';
        this.topology = null;
        this.currentView = 'overview';
        this.currentGroupId = null;
        this.refreshInterval = null;
        this.groups = [];

        // Focus. Manual: a device the operator clicked. Anomaly: the devices
        // that are offline, focused on their own (auto focus) until they
        // recover - or until the operator clears it, which holds until the
        // set of offline devices changes.
        this.manualFocus = false;
        this.anomaly = { active: false, key: '', ackKey: '', ids: [], back: null };
        this.autoFocus = this.pref('autoFocus', 'on') !== 'off';
        // Rotation: the overview and each group in turn, for a wall screen
        this.rotation = {
            on: this.pref('rotate', 'off') === 'on',
            interval: parseInt(this.pref('rotateInterval', '30'), 10) || 30,
            index: 0,
            left: 0,
        };

        this.init();
    }

    async init() {
        // Initialize topology graph
        this.topology = new TopologyGraph('#topology-svg');
        this.topology.onNodeClick = (node) => {
            this.showNodeDetails(node);
            this.focusDevice(node.id);
        };
        this.topology.onLinkClick = (link) => this.showLinkDetails(link);
        this.topology.onFocusChange = (info) => this.renderFocusBar(info);
        this.topology.getInsets = () => {
            const svg = document.getElementById('topology-svg').getBoundingClientRect();
            const bar = document.getElementById('focus-bar');
            const panel = document.getElementById('detail-panel');
            const inset = { top: 0, right: 0 };
            if (bar && !bar.hidden) inset.top = bar.getBoundingClientRect().bottom - svg.top + 8;
            if (panel && panel.style.display !== 'none') {
                inset.right = Math.max(0, svg.right - panel.getBoundingClientRect().left + 8);
            }
            return inset;
        };

        // Set up event listeners
        this.setupEventListeners();

        // Load initial data
        await this.loadTopology();
        await this.loadAlerts();
        await this.loadGroups();

        // Start auto-refresh
        this.startAutoRefresh();
        this.startRotationClock();
        this.checkAnomalies();

        // Handle highlight parameter from URL
        this.handleHighlightParam();
    }

    handleHighlightParam() {
        const params = new URLSearchParams(window.location.search);
        const highlightId = params.get('highlight');

        const focusId = params.get('focus') || highlightId;
        if (focusId) {
            // The whole map, so the device's path is on it, then focus it
            (async () => {
                if (!this.topology.nodes?.some(n => String(n.id) === String(focusId))) {
                    await this.showView('full', null);
                }
                const node = this.topology.nodes?.find(n => String(n.id) === String(focusId));
                if (node) {
                    this.showNodeDetails(node);
                    this.focusDevice(node.id);
                }
            })();

            // Clean up URL (remove the highlight param)
            history.replaceState({}, '', '/');
        }
    }

    highlightDevice(deviceId) {
        // Add pulsing animation to the highlighted device
        const svg = d3.select('#topology-svg');

        // Find the node group
        svg.selectAll('.node')
            .filter(d => d.id == deviceId || d.id == parseInt(deviceId))
            .each(function () {
                d3.select(this).select('.node-icon')
                    .classed('highlight-pulse', true)
                    .style('stroke', 'var(--warning)')
                    .style('stroke-width', '4px');

                // Remove highlight after 5 seconds
                setTimeout(() => {
                    d3.select(this).select('.node-icon')
                        .classed('highlight-pulse', false)
                        .style('stroke', null)
                        .style('stroke-width', null);
                }, 5000);
            });
    }

    setupEventListeners() {
        // Initialize i18n
        if (window.i18n) {
            i18n.init();
            const langSelect = document.getElementById('lang-select');
            if (langSelect) {
                langSelect.value = i18n.currentLang;
                langSelect.addEventListener('change', (e) => {
                    i18n.setLanguage(e.target.value);
                });
            }
        }

        // Theme toggle
        const themeToggle = document.getElementById('theme-toggle');
        if (themeToggle) {
            // Load saved theme
            const savedTheme = localStorage.getItem('theme') || 'dark';
            this.setTheme(savedTheme);

            themeToggle.addEventListener('click', () => {
                const currentTheme = document.documentElement.getAttribute('data-theme');
                const newTheme = currentTheme === 'light' ? 'dark' : 'light';
                this.setTheme(newTheme);
            });
        }

        // View selector buttons
        document.querySelectorAll('.view-btn[data-view]').forEach(btn => {
            btn.addEventListener('click', (e) => {
                this.setView(e.target.dataset.view);
            });
        });

        // Refresh button
        document.getElementById('refresh-btn')?.addEventListener('click', () => {
            this.loadTopology();
            this.checkAnomalies();
        });

        // Focus: Esc or a click on the empty map clears it
        document.addEventListener('keydown', (e) => {
            if (e.key === 'Escape' && !document.getElementById('hierarchy-modal')) {
                this.clearFocus();
                this.hideDetailPanel();
            }
        });
        const svgEl = document.getElementById('topology-svg');
        svgEl?.addEventListener('click', (e) => {
            if (e.target === svgEl) this.clearFocus();
        });
        // Looking around the map holds the rotation for a full interval
        ['wheel', 'pointerdown'].forEach(type => svgEl?.addEventListener(type, () => {
            this.rotation.left = this.rotation.interval;
        }, { passive: true }));

        const autoBtn = document.getElementById('autofocus-btn');
        const markAuto = () => autoBtn?.setAttribute('aria-pressed', String(this.autoFocus));
        autoBtn?.addEventListener('click', () => {
            this.autoFocus = !this.autoFocus;
            this.setPref('autoFocus', this.autoFocus ? 'on' : 'off');
            markAuto();
            if (this.autoFocus) this.checkAnomalies();
            else if (this.anomaly.active && !this.manualFocus) this.leaveAnomaly();
        });
        markAuto();

        const rotateBtn = document.getElementById('rotate-btn');
        const rotateSel = document.getElementById('rotate-interval');
        const markRotate = () => rotateBtn?.setAttribute('aria-pressed', String(this.rotation.on));
        if (rotateSel) rotateSel.value = String(this.rotation.interval);
        rotateBtn?.addEventListener('click', () => {
            this.rotation.on = !this.rotation.on;
            this.setPref('rotate', this.rotation.on ? 'on' : 'off');
            this.rotation.left = this.rotation.interval;
            markRotate();
            this.renderRotationStatus();
        });
        rotateSel?.addEventListener('change', () => {
            this.rotation.interval = parseInt(rotateSel.value, 10) || 30;
            this.rotation.left = this.rotation.interval;
            this.setPref('rotateInterval', String(this.rotation.interval));
            this.renderRotationStatus();
        });
        markRotate();

        // Fit button
        // Tree or free layout; the choice is remembered (topology.js)
        const layoutButtons = document.querySelectorAll('.layout-btn');
        const markLayout = () => layoutButtons.forEach(b =>
            b.classList.toggle('active', b.dataset.layout === this.topology.layoutMode));
        layoutButtons.forEach(b => b.addEventListener('click', () => {
            this.topology.setLayout(b.dataset.layout);
            markLayout();
        }));
        markLayout();

        document.getElementById('fit-btn')?.addEventListener('click', () => {
            this.topology.fitView();
        });

        // Group select
        document.getElementById('group-select')?.addEventListener('change', (e) => {
            if (e.target.value) {
                this.showView('group', parseInt(e.target.value, 10));
            }
        });

        // Panel close button
        document.getElementById('panel-close')?.addEventListener('click', () => {
            this.hideDetailPanel();
        });
    }

    setTheme(theme) {
        document.documentElement.setAttribute('data-theme', theme);
        localStorage.setItem('theme', theme);

        // Update toggle button icon
        const themeToggle = document.getElementById('theme-toggle');
        if (themeToggle) {
            themeToggle.textContent = theme === 'light' ? '☀️' : '🌙';
        }

        // Update topology colors if needed
        if (this.topology) {
            this.topology.updateTheme(theme);
        }
    }

    async setView(view) {
        if (view === 'group') {
            const groupId = parseInt(document.getElementById('group-select')?.value, 10);
            // Don't load if no group selected yet
            await this.showView(view, Number.isNaN(groupId) ? null : groupId);
        } else {
            await this.showView(view, null);
        }
    }

    // Switch the map to a view (and group), keeping the buttons in step
    async showView(view, groupId = null, { fit } = {}) {
        document.querySelectorAll('.view-btn[data-view]').forEach(btn => {
            btn.classList.toggle('active', btn.dataset.view === view);
        });
        const groupSelect = document.getElementById('group-select');
        if (groupSelect) {
            groupSelect.style.display = view === 'group' ? 'block' : 'none';
            if (view === 'group' && groupId) groupSelect.value = String(groupId);
        }
        this.currentView = view;
        this.currentGroupId = view === 'group' ? groupId : null;
        if (view === 'group' && !groupId) return;
        await this.loadTopology(view, this.currentGroupId, { fit });
    }

    async loadTopology(view = this.currentView, groupId = this.currentGroupId, { fit } = {}) {
        this.showLoading(true);

        try {
            let url = `${this.apiBase}/topology?view=${view}`;
            if (groupId) url += `&group_id=${groupId}`;

            const response = await fetch(url);
            if (!response.ok) throw new Error('Failed to load topology');

            const data = await response.json();
            // Fit the map to the view when it is a different map - another
            // view or group - but not on the minute's refresh, which would
            // throw away wherever the operator had zoomed to
            const key = `${view}:${groupId || ''}`;
            this.topology.setData(data, { fit: fit ?? key !== this.lastTopologyKey });
            this.lastTopologyKey = key;
            this.updateLastUpdate(data.last_updated);
            this.opsCounts = {
                online: (data.nodes || []).filter(n => n.status === 'managed').length,
                total: (data.nodes || []).length,
                alerts: this.opsCounts ? this.opsCounts.alerts : null
            };
            this.renderOpsStatus();


        } catch (error) {
            console.error('Error loading topology:', error);
            this.showError(t('failedToLoadTopology'));
        } finally {
            this.showLoading(false);
        }
    }

    async loadAlerts() {
        try {
            const response = await fetch(`${this.apiBase}/alerts/active`);
            if (!response.ok) throw new Error('Failed to load alerts');

            const data = await response.json();
            this.renderAlerts(data.alerts);
            if (window.AlertSound) AlertSound.notify(data.alerts);

        } catch (error) {
            console.error('Error loading alerts:', error);
        }
    }

    async loadGroups() {
        try {
            const response = await fetch(`${this.apiBase}/groups`);
            if (!response.ok) throw new Error('Failed to load groups');

            const groups = await response.json();
            this.groups = groups;
            this.renderGroupOptions(groups);

        } catch (error) {
            console.error('Error loading groups:', error);
        }
    }

    // The status line in the header: how many of the devices on this map
    // answer, how many alerts are open, and the time - the three things a
    // wall screen is glanced at for.
    renderOpsStatus() {
        const el = document.getElementById('ops-status');
        if (!el) return;
        const c = this.opsCounts || {};
        const parts = [];
        if (c.total != null) {
            const level = c.online < c.total ? 'crit' : 'ok';
            parts.push(`<span class="ops-pill ${level}"><i></i>${c.online}/${c.total} ${escapeHtml(t('online'))}</span>`);
        }
        if (c.alerts != null) {
            const level = c.alerts ? 'crit' : 'quiet';
            parts.push(`<span class="ops-pill ${level}"><i></i>${c.alerts} ${escapeHtml(t('activeAlerts'))}</span>`);
        }
        parts.push('<span class="ops-pill clock" id="ops-clock"></span>');
        el.innerHTML = parts.join('');
        this.tickOpsClock();
        if (!this.opsClockTimer) {
            this.opsClockTimer = setInterval(() => this.tickOpsClock(), 1000);
        }
    }

    tickOpsClock() {
        const el = document.getElementById('ops-clock');
        if (el) el.textContent = new Date().toLocaleTimeString([], { hour12: false });
    }

    // A 20-segment bar for one percentage, lit up to the value: green, amber
    // from 70 %, red from 90 % - the same levels the metric bars use.
    segMeter(label, value) {
        const known = value != null && !Number.isNaN(value);
        const pct = known ? Math.max(0, Math.min(100, value)) : 0;
        const level = !known ? 'unknown' : pct >= 90 ? 'critical' : pct >= 70 ? 'warning' : 'normal';
        const lit = Math.round(pct / 5);
        let cells = '';
        for (let i = 0; i < 20; i++) cells += `<i class="${i < lit ? 'on' : ''}"></i>`;
        return `
            <div class="seg-meter ${level}">
                <div class="seg-head">
                    <span>${escapeHtml(label)}</span>
                    <b>${known ? pct.toFixed(1) + '%' : 'N/A'}</b>
                </div>
                <div class="seg-cells" role="img" aria-label="${escapeHtml(label)} ${known ? pct.toFixed(1) + '%' : 'N/A'}">${cells}</div>
            </div>`;
    }

    renderAlerts(alerts) {
        const countEl = document.getElementById('alerts-count');
        const listEl = document.getElementById('alerts-list');

        if (countEl) countEl.textContent = alerts.length;
        this.opsCounts = Object.assign({}, this.opsCounts, { alerts: alerts.length });
        this.renderOpsStatus();

        if (listEl) {
            listEl.innerHTML = alerts.map(alert => `
                <div class="alert-item ${alert.severity}">
                    <span class="alert-message">${escapeHtml(alert.message || alert.alert_type)}</span>
                    <span class="alert-time">${this.formatTime(alert.triggered_at)}</span>
                </div>
            `).join('') || `<p style="padding: 0.5rem; color: var(--text-secondary);">${t('noActiveAlerts')}</p>`;
        }
    }

    renderGroupOptions(groups) {
        const select = document.getElementById('group-select');
        if (!select) return;

        select.innerHTML = `<option value="">${t('selectGroup')}</option>` +
            groups.map(g => `<option value="${g.id}">${escapeHtml(g.name)} (${g.device_count})</option>`).join('');
        if (this.currentGroupId) select.value = String(this.currentGroupId);
    }

    // -- Preferences (per browser; a blocked storage just means defaults)
    pref(key, fallback) {
        try { return localStorage.getItem(key) ?? fallback; } catch (e) { return fallback; }
    }

    setPref(key, value) {
        try { localStorage.setItem(key, value); } catch (e) { /* blocked */ }
    }

    // -- Focus -------------------------------------------------------------

    focusDevice(id) {
        this.manualFocus = true;
        this.topology.focusOn([String(id)]);
        this.renderRotationStatus();
    }

    // Esc, the clear button, a click on the empty map
    clearFocus() {
        if (this.manualFocus) {
            this.manualFocus = false;
            if (this.anomaly.active) {
                // back to the devices that are still offline
                this.topology.focusOn(this.anomaly.ids);
            } else {
                this.topology.clearFocus();
                this.checkAnomalies();
            }
        } else if (this.anomaly.active) {
            // Seen it: leave it until the set of offline devices changes
            this.anomaly.ackKey = this.anomaly.key;
            this.leaveAnomaly();
        }
        this.renderRotationStatus();
    }

    async checkAnomalies() {
        let ids;
        try {
            const response = await fetch(`${this.apiBase}/devices?status=offline&limit=0`);
            if (!response.ok) return;
            ids = (await response.json()).devices.map(d => String(d.id)).sort();
        } catch (e) {
            return;
        }
        const a = this.anomaly;
        const key = ids.join(',');
        if (!ids.length) {
            // Everything is back: the normal view again, unfocused
            a.ackKey = '';
            if (a.active) await this.leaveAnomaly();
            return;
        }
        if (!this.autoFocus || this.manualFocus || key === a.ackKey) return;
        if (!a.active) {
            a.active = true;
            a.back = { view: this.currentView, groupId: this.currentGroupId };
        }
        a.ids = ids;
        // The whole map: a group view does not carry the way up to the core
        if (this.currentView !== 'full') await this.showView('full', null, { fit: false });
        if (key !== a.key || !this.topology.focus) {
            a.key = key;
            this.topology.focusOn(ids);
        }
        this.renderRotationStatus();
    }

    async leaveAnomaly() {
        const a = this.anomaly;
        a.active = false;
        a.key = '';
        this.topology.clearFocus({ fit: false });
        const back = a.back || { view: 'overview', groupId: null };
        a.back = null;
        await this.showView(back.view, back.groupId, { fit: true });
        this.rotation.left = this.rotation.interval;
        this.renderRotationStatus();
    }

    renderFocusBar(info) {
        const bar = document.getElementById('focus-bar');
        if (!bar) return;
        if (!info) {
            bar.hidden = true;
            bar.innerHTML = '';
            return;
        }
        const auto = this.anomaly.active && !this.manualFocus;
        bar.classList.toggle('auto', auto);
        const typeName = { wlc: t('wlc'), nvr: t('nvr'), core: t('core') };
        const paths = info.paths.map(p => {
            // device, then the interfaces joining it to the next one:
            //   ap-31  Gi0/1 ⇄ Gi0/45  access-sw-01 ...
            const hops = p.chain.map((h, i) => {
                const hop = `<span class="focus-hop ${h.status === 'offline' ? 'offline' : ''}" data-id="${escapeHtml(h.id)}">${escapeHtml(h.hostname)}</span>`;
                if (i === p.chain.length - 1) return hop;
                const ports = p.hops[i];
                const via = ports && (ports.near || ports.far)
                    ? `<span class="focus-port">${escapeHtml(ports.near || '?')} ⇄ ${escapeHtml(ports.far || '?')}${ports.more ? ` +${ports.more}` : ''}</span>`
                    : '<span class="focus-arrow">→</span>';
                return hop + via;
            }).join('');
            const missing = p.found ? '' : ` <span class="focus-arrow">${escapeHtml(t('focusNoTarget').replace('{t}', typeName[p.target] || p.target))}</span>`;
            return `<div class="focus-path">${hops}${missing}</div>`;
        }).join('');
        bar.innerHTML = `
            <div class="focus-bar-head">
                <strong>${escapeHtml(t(auto ? 'focusAutoLabel' : 'focusLabel'))}</strong>
                ${info.impactCount ? `<span class="focus-impact-count">${escapeHtml(t('focusImpact').replace('{n}', info.impactCount))}</span>` : ''}
                <button class="control-btn" id="focus-clear">✕ ${escapeHtml(t('focusClear'))}</button>
            </div>
            ${paths}`;
        bar.hidden = false;
        bar.querySelector('#focus-clear').addEventListener('click', () => this.clearFocus());
        bar.querySelectorAll('.focus-hop').forEach(el => el.addEventListener('click', () => {
            const node = this.topology.nodes.find(n => String(n.id) === el.dataset.id);
            if (node) this.showNodeDetails(node);
        }));
    }

    // -- Rotation ----------------------------------------------------------

    rotationItems() {
        return [{ view: 'overview', groupId: null }].concat(
            (this.groups || []).filter(g => g.device_count > 0)
                .map(g => ({ view: 'group', groupId: g.id, name: g.name })));
    }

    rotationPaused() {
        return this.manualFocus || this.anomaly.active || document.hidden;
    }

    startRotationClock() {
        this.rotation.left = this.rotation.interval;
        this.renderRotationStatus();
        setInterval(() => {
            if (!this.rotation.on || this.rotationPaused()) { this.renderRotationStatus(); return; }
            this.rotation.left -= 1;
            if (this.rotation.left <= 0) this.rotateNext();
            this.renderRotationStatus();
        }, 1000);
    }

    async rotateNext() {
        const items = this.rotationItems();
        this.rotation.left = this.rotation.interval;
        if (items.length < 2) return;
        // carry on from wherever the map is now
        const at = items.findIndex(i => i.view === this.currentView && i.groupId === this.currentGroupId);
        this.rotation.index = ((at >= 0 ? at : this.rotation.index) + 1) % items.length;
        const next = items[this.rotation.index];
        await this.showView(next.view, next.groupId);
    }

    renderRotationStatus() {
        const el = document.getElementById('rotate-status');
        if (!el) return;
        if (!this.rotation.on) { el.textContent = ''; return; }
        const items = this.rotationItems();
        const at = items.findIndex(i => i.view === this.currentView && i.groupId === this.currentGroupId);
        const pos = `${at >= 0 ? at + 1 : '-'}/${items.length}`;
        el.textContent = this.rotationPaused() ? `${pos} ⏸` : `${pos} · ${this.rotation.left}s`;
    }

    showNodeDetails(node) {
        const panel = document.getElementById('detail-panel');
        const title = document.getElementById('panel-title');
        const content = document.getElementById('panel-content');

        if (!panel || !title || !content) return;

        title.textContent = node.hostname;
        content.innerHTML = `
            <div class="detail-section">
                <h4>${t('deviceInfo')}</h4>
                <div class="detail-row">
                    <span class="detail-label">${t('ipAddress')}</span>
                    <span class="detail-value">${escapeHtml(node.ip_address)}</span>
                </div>
                <div class="detail-row">
                    <span class="detail-label">${t('type')}</span>
                    <span class="detail-value">${node.device_type ? t(node.device_type) : t('unknown')}</span>
                </div>
                <div class="detail-row">
                    <span class="detail-label">${t('vendor')}</span>
                    <span class="detail-value">${escapeHtml(node.vendor || t('unknown'))}</span>
                </div>
                <div class="detail-row">
                    <span class="detail-label">${t('status')}</span>
                    <span class="detail-value">${t(node.status)}</span>
                </div>
            </div>
            <div class="detail-section">
                <h4>${t('metrics')}</h4>
                <div class="seg-meters">
                    ${this.segMeter(t('cpu'), node.cpu_percent)}
                    ${this.segMeter(t('memory'), node.memory_percent)}
                </div>
            </div>
            ${node.alert_count > 0 ? `
            <div class="detail-section">
                <h4>${t('alerts')} (${node.alert_count})</h4>
                <div id="device-alerts-list" style="max-height: 200px; overflow-y: auto;">
                    <p style="color: var(--text-secondary);">${t('loadingAlerts')}</p>
                </div>
            </div>
            ` : ''}
            <div class="detail-section" style="margin-top: 1rem;">
                <button onclick="window.app.showDeviceHierarchy(${node.id})" class="control-btn" style="display: block; width: 100%; text-align: center;">
                    ${t('viewFullDetails')} →
                </button>
            </div>
        `;

        panel.style.display = 'flex';

        // Load alerts for this device if any
        if (node.alert_count > 0) {
            this.loadDeviceAlerts(node.id);
        }
    }

    async loadDeviceAlerts(deviceId) {
        try {
            const response = await fetch(`${this.apiBase}/alerts?device_id=${deviceId}&is_active=true`);
            const data = await response.json();
            const alertsList = document.getElementById('device-alerts-list');

            if (alertsList && data.alerts.length > 0) {
                alertsList.innerHTML = data.alerts.map(a => `
                    <div style="padding: 0.5rem; background: var(--bg-tertiary); border-radius: 4px; margin-bottom: 0.5rem; border-left: 3px solid ${a.severity === 'critical' ? 'var(--danger)' : 'var(--warning)'};">
                        <div style="font-weight: 500; font-size: 0.875rem;">${this.formatAlertType(a.alert_type)}</div>
                        <div style="font-size: 0.75rem; color: var(--text-secondary);">${escapeHtml(a.message || t('noDetails'))}</div>
                        ${a.current_value ? `<div style="font-size: 0.75rem; color: var(--text-secondary);">${t('value')}: ${a.current_value.toFixed(1)} (${t('threshold').toLowerCase()}: ${a.threshold_value || '-'})</div>` : ''}
                    </div>
                `).join('');
            } else if (alertsList) {
                alertsList.innerHTML = `<p style="color: var(--text-secondary);">${t('noActiveAlerts')}</p>`;
            }
        } catch (error) {
            console.error('Error loading device alerts:', error);
        }
    }

    formatAlertType(type) {
        const keys = {
            'device_offline': 'deviceOffline',
            'cpu_high': 'highCpu',
            'memory_high': 'highMemory',
            'link_high_utilization': 'linkHighUtilization',
            'link_down': 'linkDown'
        };
        return keys[type] ? t(keys[type]) : type;
    }

    showLinkDetails(link) {
        const panel = document.getElementById('detail-panel');
        const title = document.getElementById('panel-title');
        const content = document.getElementById('panel-content');

        if (!panel || !title || !content) return;

        const sourceNode = this.topology.nodes.find(n => n.id === link.source.id || n.id === link.source);
        const targetNode = this.topology.nodes.find(n => n.id === link.target.id || n.id === link.target);

        title.textContent = t('linkDetails');
        content.innerHTML = `
            <div class="detail-section">
                <h4>${t('connection')}</h4>
                <div class="detail-row">
                    <span class="detail-label">${t('from')}</span>
                    <span class="detail-value">${escapeHtml(sourceNode?.hostname || t('unknown'))}</span>
                </div>
                <div class="detail-row">
                    <span class="detail-label">${t('to')}</span>
                    <span class="detail-value">${escapeHtml(targetNode?.hostname || t('unknown'))}</span>
                </div>
            </div>
            <div class="detail-section">
                <h4>${t('bandwidth')}</h4>
                <div class="detail-row">
                    <span class="detail-label">${t('total')}</span>
                    <span class="detail-value">${this.formatBandwidth(link.total_bandwidth_mbps)}</span>
                </div>
                <div class="detail-row">
                    <span class="detail-label">${t('inUtilization')}</span>
                    <span class="detail-value">${this.formatUtilization(link.utilization_in_percent)}</span>
                </div>
                <div class="detail-row">
                    <span class="detail-label">${t('outUtilization')}</span>
                    <span class="detail-value">${this.formatUtilization(link.utilization_out_percent)}</span>
                </div>
                <div class="detail-row">
                    <span class="detail-label">${t('status')}</span>
                    <span class="detail-value" style="color: var(--${this.getStatusColor(link.status)})">${t(link.status)}</span>
                </div>
            </div>
            ${link.port_details?.length ? `
            <div class="detail-section">
                <h4>${t('portDetails')}</h4>
                <div class="port-list">
                    ${link.port_details.map(p => `
                        <div class="port-item">
                            <span class="port-name">${escapeHtml(p.local_port)} ↔ ${escapeHtml(p.remote_port)}</span>
                            <span class="port-util">${this.formatBandwidth(p.bandwidth_mbps)}</span>
                        </div>
                    `).join('')}
                </div>
            </div>
            ` : ''}
        `;

        panel.style.display = 'flex';
    }

    hideDetailPanel() {
        const panel = document.getElementById('detail-panel');
        if (panel) panel.style.display = 'none';
    }

    showLoading(show) {
        const overlay = document.getElementById('loading-overlay');
        if (overlay) overlay.style.display = show ? 'flex' : 'none';
    }

    showError(message) {
        // Simple error display - can be enhanced
        console.error(message);
        alert(message);
    }

    updateLastUpdate(timestamp) {
        const el = document.getElementById('last-update');
        if (el && timestamp) {
            // Ensure UTC is correctly parsed - add Z suffix if not present
            let ts = timestamp;
            if (!ts.endsWith('Z') && !ts.includes('+')) {
                ts += 'Z';
            }
            const date = new Date(ts);
            // Only the time, so the label keeps its data-i18n span and
            // follows a language switch without waiting for a refresh.
            const timeEl = document.getElementById('last-update-time');
            if (timeEl) timeEl.textContent = date.toLocaleTimeString();
        }
    }

    formatTime(timestamp) {
        if (!timestamp) return '';
        // Ensure UTC is correctly parsed - add Z suffix if not present
        let ts = timestamp;
        if (!ts.endsWith('Z') && !ts.includes('+')) {
            ts += 'Z';
        }
        const date = new Date(ts);
        return date.toLocaleTimeString();
    }

    // null means the collector has not got two counter samples for this
    // link yet. Showing 0% would read as "idle", which is a different
    // statement from "not measured".
    formatUtilization(value) {
        return (value === null || value === undefined)
            ? '&mdash;'
            : `${value.toFixed(1)}%`;
    }

    formatBandwidth(mbps) {
        if (!mbps) return 'N/A';
        if (mbps >= 1000) return `${(mbps / 1000).toFixed(0)}G`;
        return `${mbps}M`;
    }

    getStatusColor(status) {
        const colors = {
            normal: 'success',
            elevated: 'elevated',
            warning: 'warning',
            critical: 'danger'
        };
        return colors[status] || 'text-secondary';
    }

    startAutoRefresh() {
        // Refresh every 60 seconds
        this.refreshInterval = setInterval(() => {
            this.loadTopology();
            this.loadAlerts();
        }, 60000);
        // Offline devices are looked for more often than the map is redrawn:
        // the auto focus is only as quick as this
        this.anomalyInterval = setInterval(() => this.checkAnomalies(), 15000);
    }

    stopAutoRefresh() {
        if (this.refreshInterval) {
            clearInterval(this.refreshInterval);
            this.refreshInterval = null;
        }
    }

    async showDeviceHierarchy(deviceId) {
        try {
            // Fetch hierarchy data
            const response = await fetch(`${this.apiBase}/devices/${deviceId}/hierarchy`);
            if (!response.ok) throw new Error('Failed to load hierarchy');
            const data = await response.json();

            // Also fetch alerts for this device
            const alertsResponse = await fetch(`${this.apiBase}/alerts?device_id=${deviceId}&is_active=true`);
            const alertsData = await alertsResponse.json();

            // Build modal HTML
            const device = data.device;
            const ancestors = data.ancestors;
            const children = data.children;

            const statusClass = device.status === 'offline' ? 'danger' : 'success';
            const statusBadge = `<span style="background: var(--${statusClass}); color: white; padding: 0.125rem 0.5rem; border-radius: 4px; font-size: 0.75rem; margin-left: 0.5rem;">${device.status}</span>`;

            let modalHtml = `
                <div class="hierarchy-modal-overlay" id="hierarchy-modal" onclick="window.app.closeHierarchyModal(event)">
                    <div class="hierarchy-modal" onclick="event.stopPropagation()">
                        <div class="hierarchy-modal-header">
                            <h2>${escapeHtml(device.hostname)} ${statusBadge}</h2>
                            <button class="btn-close" onclick="window.app.closeHierarchyModal()">&times;</button>
                        </div>
                        <div class="hierarchy-modal-body">
                            <!-- Device Info -->
                            <div class="hierarchy-section">
                                <h3>📋 ${t('deviceInformation')}</h3>
                                <div class="device-info-grid">
                                    <div><span class="label">${t('ipAddress')}</span><span class="value">${escapeHtml(device.ip_address)}</span></div>
                                    <div><span class="label">${t('type')}</span><span class="value">${device.device_type}</span></div>
                                    <div><span class="label">${t('vendor')}</span><span class="value">${escapeHtml(device.vendor)}</span></div>
                                    <div><span class="label">${t('model')}</span><span class="value">${escapeHtml(device.model || '-')}</span></div>
                                    <div><span class="label">${t('firmware')}</span><span class="value">${escapeHtml(device.firmware_version || '-')}</span></div>
                                    <div><span class="label">${t('cpu')}</span><span class="value">${device.cpu_percent != null ? device.cpu_percent.toFixed(1) + '%' : 'N/A'}</span></div>
                                    <div><span class="label">${t('memory')}</span><span class="value">${device.memory_percent != null ? device.memory_percent.toFixed(1) + '%' : 'N/A'}</span></div>
                                </div>
                            </div>
                            
                            <!-- Upstream Hierarchy -->
                            ${ancestors.length > 0 ? `
                            <div class="hierarchy-section">
                                <h3>⬆️ ${t('upstreamDevices')} (${ancestors.length})</h3>
                                <div class="hierarchy-tree">
                                    ${ancestors.map((a, i) => `
                                        <div class="hierarchy-item" style="margin-left: ${(ancestors.length - 1 - i) * 1.5}rem;">
                                            <span class="hierarchy-icon">${i === 0 ? '↑' : '↑'}</span>
                                            <span class="hierarchy-hostname">${escapeHtml(a.hostname)}</span>
                                            <span class="hierarchy-type">${escapeHtml(a.device_type)}</span>
                                            <span class="hierarchy-status ${a.status === 'offline' ? 'offline' : ''}">${a.status}</span>
                                        </div>
                                    `).reverse().join('')}
                                    <div class="hierarchy-item current">
                                        <span class="hierarchy-icon">●</span>
                                        <span class="hierarchy-hostname">${escapeHtml(device.hostname)}</span>
                                        <span class="hierarchy-type">${device.device_type}</span>
                                        <span class="hierarchy-status ${device.status === 'offline' ? 'offline' : ''}">${device.status}</span>
                                    </div>
                                </div>
                            </div>
                            ` : ''}
                            
                            <!-- Downstream Children -->
                            ${children.length > 0 ? `
                            <div class="hierarchy-section">
                                <h3>⬇️ ${t('downstreamDevices')} (${children.length})</h3>
                                <div class="hierarchy-children-list">
                                    ${children.map(c => `
                                        <div class="hierarchy-child">
                                            <span class="hierarchy-hostname">${escapeHtml(c.hostname)}</span>
                                            <span class="hierarchy-meta">${escapeHtml(c.ip_address)} | ${escapeHtml(c.device_type)}</span>
                                            <span class="hierarchy-status ${c.status === 'offline' ? 'offline' : ''}">${c.status}</span>
                                        </div>
                                    `).join('')}
                                </div>
                            </div>
                            ` : ''}
                            
                            <!-- Alerts -->
                            ${alertsData.alerts.length > 0 ? `
                            <div class="hierarchy-section">
                                <h3>⚠️ ${t('activeAlerts')} (${alertsData.alerts.length})</h3>
                                <div class="hierarchy-alerts-list">
                                    ${alertsData.alerts.map(a => `
                                        <div class="hierarchy-alert ${a.severity}">
                                            <strong>${this.formatAlertType(a.alert_type)}</strong>
                                            <p>${escapeHtml(a.message || t('noDetails'))}</p>
                                        </div>
                                    `).join('')}
                                </div>
                            </div>
                            ` : ''}
                        </div>
                        <div class="hierarchy-modal-footer">
                            <button class="btn-secondary" onclick="window.app.closeHierarchyModal()">${t('close')}</button>
                        </div>
                    </div>
                </div>
            `;

            // Remove existing modal if any
            const existingModal = document.getElementById('hierarchy-modal');
            if (existingModal) existingModal.remove();

            // Add modal to body
            document.body.insertAdjacentHTML('beforeend', modalHtml);

        } catch (error) {
            console.error('Error loading device hierarchy:', error);
            alert(t('failedToLoadHierarchy'));
        }
    }

    closeHierarchyModal(event) {
        if (event && event.target !== event.currentTarget) return;
        const modal = document.getElementById('hierarchy-modal');
        if (modal) modal.remove();
    }
}

// Initialize app when DOM is ready
document.addEventListener('DOMContentLoaded', () => {
    window.app = new App();
});
