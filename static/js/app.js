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
        this.refreshInterval = null;

        this.init();
    }

    async init() {
        // Initialize topology graph
        this.topology = new TopologyGraph('#topology-svg');
        this.topology.onNodeClick = (node) => this.showNodeDetails(node);
        this.topology.onLinkClick = (link) => this.showLinkDetails(link);

        // Set up event listeners
        this.setupEventListeners();

        // Load initial data
        await this.loadTopology();
        await this.loadAlerts();
        await this.loadGroups();

        // Start auto-refresh
        this.startAutoRefresh();

        // Handle highlight parameter from URL
        this.handleHighlightParam();
    }

    handleHighlightParam() {
        const params = new URLSearchParams(window.location.search);
        const highlightId = params.get('highlight');

        if (highlightId) {
            // Find and highlight the device
            const nodeId = highlightId;

            // Wait a bit for D3 to finish rendering, then highlight
            setTimeout(() => {
                // Find the node data
                const node = this.topology.nodes?.find(n => n.id === nodeId || n.id === parseInt(nodeId));

                if (node) {
                    // Show node details panel
                    this.showNodeDetails(node);

                    // Highlight in the SVG
                    this.highlightDevice(nodeId);
                }
            }, 500);

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
        document.querySelectorAll('.view-btn').forEach(btn => {
            btn.addEventListener('click', (e) => {
                this.setView(e.target.dataset.view);
            });
        });

        // Refresh button
        document.getElementById('refresh-btn')?.addEventListener('click', () => {
            this.loadTopology();
        });

        // Fit button
        document.getElementById('fit-btn')?.addEventListener('click', () => {
            this.topology.fitView();
        });

        // Group select
        document.getElementById('group-select')?.addEventListener('change', (e) => {
            if (e.target.value) {
                this.loadTopology('group', e.target.value);
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
        // Update active button
        document.querySelectorAll('.view-btn').forEach(btn => {
            btn.classList.toggle('active', btn.dataset.view === view);
        });

        // Show/hide group select
        const groupSelect = document.getElementById('group-select');
        if (groupSelect) {
            groupSelect.style.display = view === 'group' ? 'block' : 'none';
        }

        this.currentView = view;

        // For group view, get selected group ID
        if (view === 'group') {
            const groupSelect = document.getElementById('group-select');
            const groupId = groupSelect?.value;
            if (groupId) {
                await this.loadTopology(view, parseInt(groupId));
            }
            // Don't load if no group selected yet
        } else {
            await this.loadTopology(view);
        }
    }

    async loadTopology(view = this.currentView, groupId = null) {
        this.showLoading(true);

        try {
            let url = `${this.apiBase}/topology?view=${view}`;
            if (groupId) url += `&group_id=${groupId}`;

            const response = await fetch(url);
            if (!response.ok) throw new Error('Failed to load topology');

            const data = await response.json();
            this.topology.setData(data);
            this.updateLastUpdate(data.last_updated);

            // Auto-fit on initial load
            setTimeout(() => this.topology.fitView(), 500);

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

        } catch (error) {
            console.error('Error loading alerts:', error);
        }
    }

    async loadGroups() {
        try {
            const response = await fetch(`${this.apiBase}/groups`);
            if (!response.ok) throw new Error('Failed to load groups');

            const groups = await response.json();
            this.renderGroupOptions(groups);

        } catch (error) {
            console.error('Error loading groups:', error);
        }
    }

    renderAlerts(alerts) {
        const countEl = document.getElementById('alerts-count');
        const listEl = document.getElementById('alerts-list');

        if (countEl) countEl.textContent = alerts.length;

        if (listEl) {
            listEl.innerHTML = alerts.map(alert => `
                <div class="alert-item ${alert.severity}">
                    <span class="alert-message">${alert.message || alert.alert_type}</span>
                    <span class="alert-time">${this.formatTime(alert.triggered_at)}</span>
                </div>
            `).join('') || `<p style="padding: 0.5rem; color: var(--text-secondary);">${t('noActiveAlerts')}</p>`;
        }
    }

    renderGroupOptions(groups) {
        const select = document.getElementById('group-select');
        if (!select) return;

        select.innerHTML = `<option value="">${t('selectGroup')}</option>` +
            groups.map(g => `<option value="${g.id}">${g.name} (${g.device_count})</option>`).join('');
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
                    <span class="detail-value">${node.ip_address}</span>
                </div>
                <div class="detail-row">
                    <span class="detail-label">${t('type')}</span>
                    <span class="detail-value">${node.device_type ? t(node.device_type) : t('unknown')}</span>
                </div>
                <div class="detail-row">
                    <span class="detail-label">${t('vendor')}</span>
                    <span class="detail-value">${node.vendor || t('unknown')}</span>
                </div>
                <div class="detail-row">
                    <span class="detail-label">${t('status')}</span>
                    <span class="detail-value">${t(node.status)}</span>
                </div>
            </div>
            <div class="detail-section">
                <h4>${t('metrics')}</h4>
                <div class="detail-row">
                    <span class="detail-label">${t('cpu')}</span>
                    <span class="detail-value">${node.cpu_percent ? node.cpu_percent.toFixed(1) + '%' : 'N/A'}</span>
                </div>
                <div class="detail-row">
                    <span class="detail-label">${t('memory')}</span>
                    <span class="detail-value">${node.memory_percent ? node.memory_percent.toFixed(1) + '%' : 'N/A'}</span>
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
                        <div style="font-size: 0.75rem; color: var(--text-secondary);">${a.message || t('noDetails')}</div>
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
                    <span class="detail-value">${sourceNode?.hostname || t('unknown')}</span>
                </div>
                <div class="detail-row">
                    <span class="detail-label">${t('to')}</span>
                    <span class="detail-value">${targetNode?.hostname || t('unknown')}</span>
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
                            <span class="port-name">${p.local_port} ↔ ${p.remote_port}</span>
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
                            <h2>${device.hostname} ${statusBadge}</h2>
                            <button class="btn-close" onclick="window.app.closeHierarchyModal()">&times;</button>
                        </div>
                        <div class="hierarchy-modal-body">
                            <!-- Device Info -->
                            <div class="hierarchy-section">
                                <h3>📋 ${t('deviceInformation')}</h3>
                                <div class="device-info-grid">
                                    <div><span class="label">${t('ipAddress')}</span><span class="value">${device.ip_address}</span></div>
                                    <div><span class="label">${t('type')}</span><span class="value">${device.device_type}</span></div>
                                    <div><span class="label">${t('vendor')}</span><span class="value">${device.vendor}</span></div>
                                    <div><span class="label">${t('model')}</span><span class="value">${device.model || '-'}</span></div>
                                    <div><span class="label">${t('firmware')}</span><span class="value">${device.firmware_version || '-'}</span></div>
                                    <div><span class="label">${t('cpu')}</span><span class="value">${device.cpu_percent ? device.cpu_percent.toFixed(1) + '%' : 'N/A'}</span></div>
                                    <div><span class="label">${t('memory')}</span><span class="value">${device.memory_percent ? device.memory_percent.toFixed(1) + '%' : 'N/A'}</span></div>
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
                                            <span class="hierarchy-hostname">${a.hostname}</span>
                                            <span class="hierarchy-type">${a.device_type}</span>
                                            <span class="hierarchy-status ${a.status === 'offline' ? 'offline' : ''}">${a.status}</span>
                                        </div>
                                    `).reverse().join('')}
                                    <div class="hierarchy-item current">
                                        <span class="hierarchy-icon">●</span>
                                        <span class="hierarchy-hostname">${device.hostname}</span>
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
                                            <span class="hierarchy-hostname">${c.hostname}</span>
                                            <span class="hierarchy-meta">${c.ip_address} | ${c.device_type}</span>
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
                                            <p>${a.message || t('noDetails')}</p>
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
