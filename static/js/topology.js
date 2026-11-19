/**
 * D3.js Topology Visualization
 * Network topology graph with Cisco-style device icons
 */

class TopologyGraph {
    constructor(containerId) {
        this.container = d3.select(containerId);
        this.svg = this.container;
        this.width = 0;
        this.height = 0;
        this.nodes = [];
        this.links = [];
        this.simulation = null;
        this.g = null;
        this.zoom = null;

        this.onNodeClick = null;
        this.onLinkClick = null;

        // Device icons (Cisco-style SVG paths)
        // Device icons: tactical line art, one stroke weight, drawn in
        // currentColor so the stylesheet colours each by the device's status.
        //   .l  outline      .t  tinted fill      .s  solid
        //   .k  knocked out of a solid (the panel colour)
        //   .tag / .tag-text  the designator plate, e.g. "L3"
        // Chamfered corners rather than rounded ones throughout.
        const icon = (body, tag) => `<svg viewBox="0 0 64 64" width="52" height="52" class="dev-icon" aria-hidden="true">${body}${tag ? `
                <path class="tag" d="M40 3 H58 V11 L55 14 H40 Z"/>
                <text class="tag-text" x="48.5" y="11.2" text-anchor="middle">${tag}</text>` : ''}
            </svg>`;
        const l2Body = `
                <path class="t l" d="M8 18 H52 L56 22 V46 H12 L8 42 Z"/>
                <path class="l" d="M14 25 H44 M39 21 L44 25 L39 29"/>
                <path class="l" d="M50 33 H20 M25 29 L20 33 L25 37"/>
                <path class="s" d="M14 40 h5 v3 h-5 z M22 40 h5 v3 h-5 z M30 40 h5 v3 h-5 z M38 40 h5 v3 h-5 z M46 40 h5 v3 h-5 z"/>`;
        const l3Body = `
                <path class="t l" d="M8 18 H52 L56 22 V46 H12 L8 42 Z"/>
                <path class="l" d="M32 21 V27 M29 24 L32 21 L35 24 M32 31 V37 M29 34 L32 37 L35 34"/>
                <path class="l" d="M14 29 H24 M21 26 L24 29 L21 32 M50 29 H40 M43 26 L40 29 L43 32"/>
                <path class="s" d="M14 40 h5 v3 h-5 z M22 40 h5 v3 h-5 z M30 40 h5 v3 h-5 z M38 40 h5 v3 h-5 z M46 40 h5 v3 h-5 z"/>`;
        this.deviceIcons = {
            // An octagon with traffic crossing it both ways
            router: icon(`
                <path class="t l" d="M23 9 H41 L55 23 V41 L41 55 H23 L9 41 V23 Z"/>
                <path class="l" d="M14 32 H27 M23 28 L27 32 L23 36 M50 32 H37 M41 28 L37 32 L41 36"/>
                <path class="l" d="M32 27 V14 M28 18 L32 14 L36 18 M32 37 V50 M28 46 L32 50 L36 46"/>
                <rect class="s" x="29.5" y="29.5" width="5" height="5"/>`),
            // Layer 3: routing arrows on a switch chassis. Core is stacked.
            core: icon(`
                <path class="l" d="M12 17 V12 H36"/>${l3Body}`, 'L3'),
            distribution: icon(l3Body, 'L3'),
            // Layer 2: the two opposed forwarding arrows
            access: icon(l2Body, 'L2'),
            // A shield, walled in brick
            firewall: icon(`
                <path class="t l" d="M32 6 L53 13 V31 C53 43 44 52 32 58 C20 52 11 43 11 31 V13 Z"/>
                <path class="l" d="M15 23 H49 M15 34 H49 M19 45 H45 M32 13 V23 M24 23 V34 M40 23 V34 M32 34 V45"/>`),
            // A lattice radio mast, radiating
            ap: icon(`
                <path class="l" d="M32 30 L25 56 M32 30 L39 56 M27 48 H37 M28.5 41 H35.5 M22 56 H42"/>
                <rect class="s" x="29" y="23" width="6" height="6"/>
                <path class="l" d="M25.6 19.6 A9 9 0 0 0 25.6 32.4 M38.4 19.6 A9 9 0 0 1 38.4 32.4"/>
                <path class="l" d="M20.7 14.7 A16 16 0 0 0 20.7 37.3 M43.3 14.7 A16 16 0 0 1 43.3 37.3"/>`),
            // A housing on a wall arm, lens forward, recording lamp lit
            camera: icon(`
                <path class="l" d="M12 20 L36 17"/>
                <path class="t l" d="M14 24 L50 19 L52 35 L16 40 Z"/>
                <path class="l" d="M14 26 H8 V38 H15"/>
                <rect class="s" x="43" y="24" width="4" height="4"/>
                <path class="l" d="M36 37 L39 49"/>
                <path class="t l" d="M30 49 H48 V55 H30 Z"/>`, 'CAM'),
            // One feed in, fanned out to three
            loadbalancer: icon(`
                <path class="t l" d="M8 18 H52 L56 22 V52 H12 L8 48 Z"/>
                <path class="l" d="M13 35 H24"/>
                <rect class="s" x="23" y="31.5" width="7" height="7"/>
                <path class="l" d="M30 35 L44 25 M30 35 H44 M30 35 L44 45"/>
                <path class="l" d="M41 22 L46 24 L43 28 M41 32 L46 35 L41 38 M43 42 L46 46 L41 48"/>`, 'LB'),
            // A rack unit stack with drive bays
            server: icon(`
                <path class="t l" d="M16 6 H44 L48 10 V58 H16 Z"/>
                <path class="l" d="M16 19 H48 M16 32 H48 M16 45 H48"/>
                <path class="l" d="M21 12.5 H33 M21 25.5 H33 M21 38.5 H33"/>
                <rect class="s" x="39" y="11" width="4" height="3"/>
                <rect class="s" x="39" y="24" width="4" height="3"/>
                <rect class="s" x="39" y="37" width="4" height="3"/>
                <path class="l" d="M22 50 V54 M27 50 V54 M32 50 V54 M37 50 V54 M42 50 V54"/>`),
            // A dashed frame - not a box you can touch - around a unit cube
            vm: icon(`
                <path class="l dash" d="M36 8 H8 V50 L14 56 H56 V18"/>
                <path class="t l" d="M32 17 L46 24 V40 L32 47 L18 40 V24 Z"/>
                <path class="l" d="M18 24 L32 31 L46 24 M32 31 V47"/>`, 'VM'),
            unknown: icon(`
                <path class="l dash" d="M14 12 H46 L52 18 V52 H18 L12 46 V12 Z"/>
                <text class="glyph" x="32" y="42" text-anchor="middle">?</text>`)
        };

        this.init();
    }

    init() {
        this.measure();

        this.zoom = d3.zoom()
            .scaleExtent([0.1, 4])
            .on('zoom', (event) => {
                this.g.attr('transform', event.transform);
                // A zoom or pan by hand: from here on the view is the
                // operator's, and the automatic fit leaves it alone
                if (event.sourceEvent) this.autoFit = false;
            });

        this.svg.call(this.zoom);

        this.g = this.svg.append('g');

        this.linksGroup = this.g.append('g').attr('class', 'links');
        this.nodesGroup = this.g.append('g').attr('class', 'nodes');

        this.simulation = d3.forceSimulation()
            .force('link', d3.forceLink().id(d => d.id).distance(180))
            .force('charge', d3.forceManyBody().strength(-600))
            .force('center', d3.forceCenter(this.width / 2, this.height / 2))
            .force('collision', d3.forceCollide().radius(80));

        // Once the layout has come to rest, fit it to the view - unless the
        // operator has moved the view in the meantime
        this.simulation.on('end.fit', () => {
            if (this.autoFit) this.fitView();
            this.autoFit = false;
        });

        // The map's size is read from the page, not assumed: it changes after
        // load (the alerts bar fills in below it), with the window, and when
        // the side panel opens. A size read once at start-up made "fit"
        // aim at an area taller than the one on screen.
        if (window.ResizeObserver) {
            new ResizeObserver(() => this.handleResize()).observe(this.svg.node());
        } else {
            window.addEventListener('resize', () => this.handleResize());
        }
    }

    // The map's current size on the page
    measure() {
        const rect = this.svg.node().getBoundingClientRect();
        const width = rect.width || 800;
        const height = rect.height || 600;
        const changed = width !== this.width || height !== this.height;
        this.width = width;
        this.height = height;
        return changed;
    }

    handleResize() {
        if (!this.measure()) return;
        this.simulation.force('center', d3.forceCenter(this.width / 2, this.height / 2));
        if (this.nodes && this.nodes.length) this.simulation.alpha(0.3).restart();
    }

    // fit: frame the map once its layout has come to rest
    setData(data, { fit = true } = {}) {
        // Devices already on the map stay where they are: the refresh then
        // only nudges the layout instead of laying it out from scratch
        const placed = new Map((this.nodes || []).map(n => [n.id, n]));
        this.nodes = data.nodes || [];
        this.carriedOver = this.nodes.length > 0 && this.nodes.every(n => placed.has(n.id));
        for (const n of this.nodes) {
            const old = placed.get(n.id);
            if (old) Object.assign(n, { x: old.x, y: old.y, vx: 0, vy: 0, fx: old.fx, fy: old.fy });
        }
        this.links = data.links || [];
        this.render();
        this.autoFit = fit;
    }

    getDeviceType(node) {
        const type = (node.device_type || '').toLowerCase();
        const exact = {
            core: 'core', distribution: 'distribution', dist: 'distribution', l3: 'distribution',
            access: 'access', tor: 'access', switch: 'access', l2: 'access',
            router: 'router', firewall: 'firewall', ap: 'ap', wireless: 'ap',
            camera: 'camera', cctv: 'camera',
            loadbalancer: 'loadbalancer', load_balancer: 'loadbalancer', lb: 'loadbalancer',
            server: 'server', vm: 'vm', virtual_machine: 'vm'
        };
        if (exact[type]) return exact[type];
        if (type.includes('core')) return 'core';
        if (type.includes('dist')) return 'distribution';
        if (type.includes('router')) return 'router';
        if (type.includes('firewall')) return 'firewall';
        if (type.includes('camera') || type.includes('cctv')) return 'camera';
        if (type.includes('balanc')) return 'loadbalancer';
        if (type.includes('server')) return 'server';
        if (type.includes('virtual')) return 'vm';
        if (type.includes('access') || type.includes('switch')) return 'access';
        if (type.includes('wireless')) return 'ap';
        return 'unknown';
    }

    formatVendor(vendor) {
        const vendors = {
            'cisco_ios': 'Cisco IOS',
            'cisco_nxos': 'Cisco NX-OS',
            'hp_aruba': 'HP/Aruba',
            'fortinet': 'Fortinet',
            'paloalto': 'Palo Alto',
            'ruckus': 'Ruckus'
        };
        return vendors[vendor] || vendor || 'Unknown';
    }

    render() {
        this.linksGroup.selectAll('*').remove();
        this.nodesGroup.selectAll('*').remove();

        if (this.nodes.length === 0) return;

        // Render links
        const linkElements = this.linksGroup.selectAll('g.link-group')
            .data(this.links)
            .join('g')
            .attr('class', 'link-group');

        linkElements.append('line')
            .attr('class', d => `link status-${d.status}`)
            .attr('stroke-width', d => this.getLinkWidth(d.total_bandwidth_mbps))
            .on('click', (event, d) => {
                if (this.onLinkClick) this.onLinkClick(d);
            });

        // Link bandwidth labels
        linkElements.append('text')
            .attr('class', 'link-label')
            .attr('text-anchor', 'middle')
            .attr('dy', -8)
            .text(d => this.formatBandwidth(d.total_bandwidth_mbps));

        // Link utilization labels
        linkElements.append('text')
            .attr('class', 'link-util')
            .attr('text-anchor', 'middle')
            .attr('dy', 12)
            .text(d => {
                const maxUtil = Math.max(d.utilization_in_percent || 0, d.utilization_out_percent || 0);
                return `${maxUtil.toFixed(0)}%`;
            });

        // Render nodes
        const nodeElements = this.nodesGroup.selectAll('g.node')
            .data(this.nodes)
            .join('g')
            .attr('class', d => `node device-${this.getDeviceType(d)} status-${d.status === 'managed' ? 'online' : d.status}`)
            .call(this.drag())
            .on('click', (event, d) => {
                if (this.onNodeClick) this.onNodeClick(d);
            });

        // The plate the icon sits on: a square frame in the device's status
        // colour (the stylesheet colours it, by the node's status class)
        nodeElements.append('rect')
            .attr('class', 'node-plate')
            .attr('x', -29)
            .attr('y', -29)
            .attr('width', 58)
            .attr('height', 58);

        // Device icon using foreignObject
        nodeElements.append('foreignObject')
            .attr('width', 52)
            .attr('height', 52)
            .attr('x', -26)
            .attr('y', -26)
            .html(d => this.deviceIcons[this.getDeviceType(d)] || this.deviceIcons.unknown);

        // Offline overlay with blinking animation
        nodeElements.filter(d => d.status === 'offline')
            .append('rect')
            .attr('class', 'offline-blink')
            .attr('x', -26)
            .attr('y', -26)
            .attr('width', 52)
            .attr('height', 52)
            .attr('rx', 4)
            .attr('fill', 'rgba(248, 81, 73, 0.5)')
            .attr('stroke', '#f85149')
            .attr('stroke-width', 2);

        // Vendor label (above icon)
        nodeElements.append('text')
            .attr('dy', -35)
            .attr('text-anchor', 'middle')
            .attr('class', 'node-vendor')
            .attr('fill', '#8b949e')
            .attr('font-size', '10px')
            .text(d => this.formatVendor(d.vendor));

        // Hostname label
        nodeElements.append('text')
            .attr('dy', 40)
            .attr('text-anchor', 'middle')
            .attr('class', 'node-hostname')
            .attr('fill', '#e6edf3')
            .attr('font-size', '12px')
            .attr('font-weight', 'bold')
            .text(d => d.hostname);

        // IP address label
        nodeElements.append('text')
            .attr('dy', 54)
            .attr('text-anchor', 'middle')
            .attr('class', 'node-ip')
            .attr('fill', '#8b949e')
            .attr('font-size', '10px')
            .text(d => d.ip_address);

        // Alert badge
        nodeElements.filter(d => d.alert_count > 0)
            .append('circle')
            .attr('cx', 22)
            .attr('cy', -22)
            .attr('r', 10)
            .attr('fill', '#f85149')
            .attr('stroke', '#da3633')
            .attr('stroke-width', 2);

        nodeElements.filter(d => d.alert_count > 0)
            .append('text')
            .attr('x', 22)
            .attr('y', -22)
            .attr('text-anchor', 'middle')
            .attr('dominant-baseline', 'central')
            .attr('font-size', '11px')
            .attr('font-weight', 'bold')
            .attr('fill', 'white')
            .text(d => d.alert_count);

        // Simulation tick handler
        this.simulation.nodes(this.nodes).on('tick', () => {
            linkElements.select('line')
                .attr('x1', d => d.source.x)
                .attr('y1', d => d.source.y)
                .attr('x2', d => d.target.x)
                .attr('y2', d => d.target.y);

            linkElements.selectAll('text')
                .attr('x', d => (d.source.x + d.target.x) / 2)
                .attr('y', d => (d.source.y + d.target.y) / 2);

            nodeElements.attr('transform', d => `translate(${d.x}, ${d.y})`);
        });

        this.simulation.force('link').links(this.links);
        this.simulation.alpha(this.carriedOver ? 0.1 : 1).restart();
    }

    drag() {
        return d3.drag()
            .on('start', (event, d) => {
                if (!event.active) this.simulation.alphaTarget(0.3).restart();
                this.autoFit = false;
                d.fx = d.x;
                d.fy = d.y;
            })
            .on('drag', (event, d) => {
                d.fx = event.x;
                d.fy = event.y;
            })
            .on('end', (event, d) => {
                if (!event.active) this.simulation.alphaTarget(0);
                d.fx = null;
                d.fy = null;
            });
    }

    getLinkWidth(bandwidthMbps) {
        if (bandwidthMbps >= 100000) return 10;  // 100G
        if (bandwidthMbps >= 40000) return 8;   // 40G
        if (bandwidthMbps >= 10000) return 6;   // 10G
        if (bandwidthMbps >= 1000) return 4;    // 1G
        return 2;
    }

    formatBandwidth(mbps) {
        if (mbps >= 1000) return `${mbps / 1000}G`;
        return `${mbps}M`;
    }

    fitView() {
        if (this.nodes.length === 0) return;
        this.measure();

        const bounds = this.g.node().getBBox();
        const padding = 80;

        const scale = Math.min(
            this.width / (bounds.width + padding * 2),
            this.height / (bounds.height + padding * 2)
        );

        const centerX = bounds.x + bounds.width / 2;
        const centerY = bounds.y + bounds.height / 2;

        this.svg.transition().duration(750).call(
            this.zoom.transform,
            d3.zoomIdentity
                .translate(this.width / 2, this.height / 2)
                .scale(Math.min(scale, 1))
                .translate(-centerX, -centerY)
        );
    }

    resetZoom() {
        this.svg.transition().duration(500).call(
            this.zoom.transform,
            d3.zoomIdentity
        );
    }

    updateTheme(theme) {
        // Update text colors based on theme
        const textColor = theme === 'light' ? '#1f2328' : '#e6edf3';
        const secondaryColor = theme === 'light' ? '#656d76' : '#8b949e';
        const linkLabelColor = theme === 'light' ? '#0969da' : '#58a6ff';

        this.nodesGroup.selectAll('.node-hostname')
            .attr('fill', textColor);

        this.nodesGroup.selectAll('.node-vendor')
            .attr('fill', secondaryColor);

        this.nodesGroup.selectAll('.node-ip')
            .attr('fill', secondaryColor);

        this.linksGroup.selectAll('.link-label')
            .attr('fill', linkLabelColor);

        this.linksGroup.selectAll('.link-util')
            .attr('fill', secondaryColor);
    }
}

window.TopologyGraph = TopologyGraph;
