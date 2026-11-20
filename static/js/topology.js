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

        // 'tree' (devices hung under their upstream device, the default) or
        // 'force' (the free layout). Remembered per browser.
        let saved = null;
        try { saved = localStorage.getItem('topologyLayout'); } catch (e) { /* blocked */ }
        this.layoutMode = saved === 'force' ? 'force' : 'tree';
        this.treeBuses = [];

        // Focus: the devices being looked at (ids), what they depend on and
        // what depends on them. Survives the minute's refresh.
        this.focusIds = null;
        this.focus = null;
        this.onFocusChange = null;

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
            // A controller chassis with the radio it manages rising from it
            wlc: icon(`
                <path class="t l" d="M8 30 H52 L56 34 V54 H12 L8 50 Z"/>
                <path class="s" d="M14 47 h5 v3 h-5 z M22 47 h5 v3 h-5 z M30 47 h5 v3 h-5 z"/>
                <path class="l" d="M38 40 H50 M38 45 H46"/>
                <path class="l" d="M32 30 V22"/>
                <rect class="s" x="29" y="17" width="6" height="6"/>
                <path class="l" d="M25.3 13.3 A9.5 9.5 0 0 0 25.3 26.7 M38.7 13.3 A9.5 9.5 0 0 1 38.7 26.7"/>
                <path class="l" d="M20.4 8.4 A16.5 16.5 0 0 0 20.4 31.6 M43.6 8.4 A16.5 16.5 0 0 1 43.6 31.6"/>`, 'WLC'),
            // A recorder: drive bays, a reel of footage, the record lamp
            nvr: icon(`
                <path class="t l" d="M8 18 H52 L56 22 V50 H12 L8 46 Z"/>
                <path class="l" d="M14 24 H34 V44 H14 Z"/>
                <path class="l" d="M14 29 H34 M14 34 H34 M14 39 H34"/>
                <circle class="l" cx="45" cy="31" r="6"/>
                <circle class="s" cx="45" cy="31" r="2"/>
                <path class="l" d="M39 42 H51"/>
                <rect class="s" x="48" y="45" width="3" height="3"/>`, 'NVR'),
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
        // The focused path, drawn over the faded map and under the devices
        this.focusGroup = this.g.append('g').attr('class', 'focus-routes');
        this.nodesGroup = this.g.append('g').attr('class', 'nodes');

        this.simulation = d3.forceSimulation()
            .force('link', d3.forceLink().id(d => d.id).distance(180))
            .force('charge', d3.forceManyBody().strength(-600))
            .force('center', d3.forceCenter(this.width / 2, this.height / 2))
            .force('collision', d3.forceCollide().radius(80))
            // A weak pull toward the middle. forceCenter only moves the whole
            // picture; without this, parts of a map that are not linked to
            // each other (a group of two separate subtrees, say) push each
            // other away for ever, and fitting the view then shrinks the
            // devices to specks.
            .force('x', d3.forceX(this.width / 2).strength(0.06))
            .force('y', d3.forceY(this.height / 2).strength(0.06));

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
        if (this.layoutMode === 'tree') return;
        this.simulation.force('center', d3.forceCenter(this.width / 2, this.height / 2));
        this.simulation.force('x').x(this.width / 2);
        this.simulation.force('y').y(this.height / 2);
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
        // The tree is laid out at once, there is no simulation to wait for
        if (this.layoutMode === 'tree' && fit) {
            this.autoFit = false;
            this.fitView();
        }
    }

    getDeviceType(node) {
        const type = (node.device_type || '').toLowerCase();
        const exact = {
            core: 'core', distribution: 'distribution', dist: 'distribution', l3: 'distribution',
            access: 'access', tor: 'access', switch: 'access', l2: 'access',
            router: 'router', firewall: 'firewall', ap: 'ap', wireless: 'ap',
            camera: 'camera', cctv: 'camera',
            nvr: 'nvr', dvr: 'nvr',
            wlc: 'wlc', wireless_controller: 'wlc', controller: 'wlc',
            loadbalancer: 'loadbalancer', load_balancer: 'loadbalancer', lb: 'loadbalancer',
            server: 'server', vm: 'vm', virtual_machine: 'vm'
        };
        if (exact[type]) return exact[type];
        if (type.includes('core')) return 'core';
        if (type.includes('dist')) return 'distribution';
        if (type.includes('router')) return 'router';
        if (type.includes('firewall')) return 'firewall';
        if (type.includes('nvr') || type.includes('recorder')) return 'nvr';
        if (type.includes('controller') || type.includes('wlc')) return 'wlc';
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
        this.focusGroup.selectAll('*').remove();

        if (this.nodes.length === 0) { this.refreshFocus(); return; }

        const tree = this.layoutMode === 'tree';
        if (tree) {
            this.simulation.stop();
            this.computeTreeLayout();
            // The shared trunk and buses, under the links
            this.linksGroup.append('g').attr('class', 'tree-buses')
                .selectAll('path').data(this.treeBuses).join('path')
                .attr('class', 'tree-bus').attr('d', d => d);
        }

        // Render links
        const linkElements = this.linksGroup.selectAll('g.link-group')
            .data(this.links)
            .join('g')
            .attr('class', d => `link-group${tree && !d.inTree ? ' link-extra' : ''}`);

        linkElements.append('path')
            .attr('class', d => `link status-${d.status}`)
            .attr('stroke-width', d => this.getLinkWidth(d.total_bandwidth_mbps))
            .on('click', (event, d) => {
                if (this.onLinkClick) this.onLinkClick(d);
            });

        const utilization = d => Math.max(d.utilization_in_percent || 0, d.utilization_out_percent || 0);
        if (tree) {
            // One small label beside each uplink, not two in the middle of it
            linkElements.filter(d => d.inTree).append('text')
                .attr('class', 'link-label tree-link-label')
                .attr('x', d => d.labelX)
                .attr('y', d => d.labelY)
                .text(d => `${this.formatBandwidth(d.total_bandwidth_mbps)} ${utilization(d).toFixed(0)}%`);
        } else {
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
                .text(d => `${utilization(d).toFixed(0)}%`);
        }

        // Render nodes
        const nodeElements = this.nodesGroup.selectAll('g.node')
            .data(this.nodes)
            .join('g')
            .attr('class', d => `node device-${this.getDeviceType(d)} status-${d.status === 'managed' ? 'online' : d.status}`)
            .call(tree ? () => {} : this.drag())
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

        // Vendor label (above icon; free layout only, see TREE above)
        nodeElements.filter(() => !tree).append('text')
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

        if (tree) {
            // A link outside the tree (a second uplink, core to core) is shown
            // while either of its devices is under the pointer, and always
            // when it is down - otherwise these are what tangle the picture
            const endOf = e => (typeof e === 'object' ? e.id : e);
            nodeElements
                .on('mouseenter.extra', (event, d) => linkElements
                    .classed('show', l => endOf(l.source) === d.id || endOf(l.target) === d.id))
                .on('mouseleave.extra', () => linkElements.classed('show', false));
            const byId = new Map(this.nodes.map(n => [n.id, n]));
            const end = e => (typeof e === 'object' ? e : byId.get(e));
            linkElements.select('path').attr('d', d => d.route ||
                `M${end(d.source).x},${end(d.source).y} L${end(d.target).x},${end(d.target).y}`);
            nodeElements.attr('transform', d => `translate(${d.x}, ${d.y})`);
            this.refreshFocus();
            return;
        }

        // Simulation tick handler
        this.simulation.nodes(this.nodes).on('tick', () => {
            linkElements.select('path')
                .attr('d', d => `M${d.source.x},${d.source.y} L${d.target.x},${d.target.y}`);

            linkElements.selectAll('text')
                .attr('x', d => (d.source.x + d.target.x) / 2)
                .attr('y', d => (d.source.y + d.target.y) / 2);

            nodeElements.attr('transform', d => `translate(${d.x}, ${d.y})`);
            if (this.focus) this.drawFocusRoutes();
        });

        this.simulation.force('link').links(this.links);
        this.simulation.alpha(this.carriedOver ? 0.1 : 1).restart();
        this.refreshFocus();
    }

    // -- Tree layout -----------------------------------------------------------
    //
    // A force layout knows nothing about up and down: a 100-device map of what
    // is in reality a tree came out with 196 crossing lines and 40 lines run
    // through devices they have nothing to do with. This lays the tree out as
    // a compact org chart instead:
    //
    //   * each device hangs under its upstream device (parent_id, from
    //     discovery); the cores are the roots
    //   * a parent sits at the top left of its block, its children in rows
    //     below and indented, joined by a right-angled bus - no tree link can
    //     cross another or pass through a device
    //   * a parent with many children wraps them into several rows, so the
    //     picture stays close to the screen's shape instead of becoming one
    //     very long row that the fit then shrinks to unreadable
    //   * links that are not part of the tree (a distribution switch's second
    //     uplink, the core-to-core link) are drawn as faint dashed lines
    //
    // Positions follow from the structure, so the same network is drawn the
    // same way every time.

    static TREE = {
        // A device's box: the icon, name and IP below it. The vendor is not
        // drawn in the tree (it is in the device panel): 100 devices fitted
        // to one screen cannot spare the line.
        W: 126,
        H: 92,
        CY: 33,          // the icon's centre, from the top of the box
        INDENT: 32,      // children start this far right of their parent
        GAP_X: 14,
        GAP_Y: 34,       // the bus runs in this gap, above each row
        TRUNK: 8,        // the trunk's x, from the left of the parent's box
    };

    static TYPE_ORDER = {
        core: 0, router: 1, firewall: 2, distribution: 3, loadbalancer: 4, wlc: 5,
        nvr: 6, server: 7, access: 8, vm: 9, ap: 10, camera: 11, unknown: 12
    };

    setLayout(mode) {
        this.layoutMode = mode === 'force' ? 'force' : 'tree';
        try { localStorage.setItem('topologyLayout', this.layoutMode); } catch (e) { /* blocked */ }
        // A fresh layout either way: carried-over positions belong to the other one
        for (const n of this.nodes) { n.fx = null; n.fy = null; }
        this.carriedOver = false;
        this.render();
        if (this.focus && this.layoutMode === 'tree') this.zoomToFocus();
        else if (this.layoutMode === 'tree') this.fitView();
        else this.autoFit = !this.focus;
    }

    // Which link is which device's uplink, and every device's children
    buildTree() {
        const byId = new Map(this.nodes.map(n => [n.id, n]));
        const adjacency = new Map(this.nodes.map(n => [n.id, []]));
        const endId = e => (typeof e === 'object' ? e.id : e);
        for (const link of this.links) {
            const a = endId(link.source), b = endId(link.target);
            if (adjacency.has(a) && adjacency.has(b)) {
                adjacency.get(a).push([b, link]);
                adjacency.get(b).push([a, link]);
            }
        }
        const rank = n => TopologyGraph.TYPE_ORDER[this.getDeviceType(n)] ?? 12;
        const parent = new Map();
        const uplink = new Map();

        // 1. The upstream device discovery recorded, when it is on this map
        //    and actually cabled to the device
        for (const n of this.nodes) {
            if (!n.parent_id || !byId.has(n.parent_id)) continue;
            const via = adjacency.get(n.id).find(([other]) => other === n.parent_id);
            if (via) { parent.set(n.id, n.parent_id); uplink.set(n.id, via[1]); }
        }
        // ...without cycles (a stale parent_id can point back down the tree)
        for (const n of this.nodes) {
            const seen = new Set([n.id]);
            let cur = parent.get(n.id);
            while (cur !== undefined) {
                if (seen.has(cur)) { parent.delete(n.id); uplink.delete(n.id); break; }
                seen.add(cur);
                cur = parent.get(cur);
            }
        }
        // 2. Everything else: breadth first from the devices with no parent,
        //    highest tier first, so a device hangs under the closest one.
        //    A core with no recorded parent is a root in its own right: the
        //    core-to-core link must not hang one core under the other.
        const roots = this.nodes.filter(n => !parent.has(n.id)).sort((a, b) => rank(a) - rank(b) ||
            adjacency.get(b.id).length - adjacency.get(a.id).length);
        const placed = new Set();
        for (const r of roots) if (this.getDeviceType(r) === 'core') placed.add(r.id);
        const markTree = id => {
            const stack = [id];
            while (stack.length) {
                const cur = stack.pop();
                if (placed.has(cur)) continue;
                placed.add(cur);
                for (const n of this.nodes) if (parent.get(n.id) === cur) stack.push(n.id);
            }
        };
        const realRoots = [];
        for (const r of roots) {
            const isCore = this.getDeviceType(r) === 'core';
            if (placed.has(r.id) && !isCore) continue;
            if (isCore && realRoots.includes(r.id)) continue;
            realRoots.push(r.id);
            placed.delete(r.id);
            markTree(r.id);
            const queue = [r.id];
            while (queue.length) {
                const cur = queue.shift();
                const sub = [cur];
                // the whole subtree already hanging under cur is reachable too
                for (let i = 0; i < sub.length; i++) {
                    for (const n of this.nodes) if (parent.get(n.id) === sub[i]) sub.push(n.id);
                }
                for (const s of sub) {
                    for (const [other, link] of adjacency.get(s)) {
                        // never re-parent a device whose upstream discovery
                        // recorded (it hangs under its own root's walk)
                        if (placed.has(other) || parent.has(other)) continue;
                        parent.set(other, s);
                        uplink.set(other, link);
                        markTree(other);
                        queue.push(other);
                    }
                }
            }
        }
        const children = new Map(this.nodes.map(n => [n.id, []]));
        for (const n of this.nodes) if (parent.has(n.id)) children.get(parent.get(n.id)).push(n.id);
        const natural = (a, b) => byId.get(a).hostname.localeCompare(byId.get(b).hostname, undefined, { numeric: true });
        for (const list of children.values()) {
            list.sort((a, b) => rank(byId.get(a)) - rank(byId.get(b)) || natural(a, b));
        }
        realRoots.sort((a, b) => rank(byId.get(a)) - rank(byId.get(b)) || natural(a, b));
        const treeLinks = new Set(uplink.values());
        return { byId, roots: realRoots, children, uplink, treeLinks };
    }

    computeTreeLayout() {
        const T = TopologyGraph.TREE;
        const { byId, roots, children, uplink, treeLinks } = this.buildTree();
        const endId = e => (typeof e === 'object' ? e.id : e);
        const blocks = new Map();
        // Every block aims for the shape of the map on screen, so that when
        // the whole tree is fitted it fills the space instead of a corner of it
        const screenAspect = Math.max(1.3, this.width / Math.max(1, this.height));
        let screen = screenAspect;

        // A child group is either a device with its own subtree (a block),
        // or a column of end devices - access points, cameras, servers -
        // stacked under one another on a vertical line, like a file tree.
        // Stacking the ends is what keeps the picture dense: a row of them
        // would make every row as wide as the longest list of APs.
        const STACK = 8;                           // between stacked devices
        const MAX_STACK = 4;                       // devices per column
        const items = new Map();                   // id or 'col:<parent>:<i>' -> {w, h, ids?}
        const sizeOf = key => items.get(key) || blocks.get(key);

        const rowsOf = (keys, limit) => {
            const rows = [];
            let row = [], width = 0;
            for (const key of keys) {
                const w = sizeOf(key).w;
                if (row.length && width + T.GAP_X + w > limit) { rows.push(row); row = []; width = 0; }
                width += (row.length ? T.GAP_X : 0) + w;
                row.push(key);
            }
            if (row.length) rows.push(row);
            return rows;
        };
        const limitFor = (keys, aspect) => {
            const area = keys.reduce((s, k) => s + (sizeOf(k).w + T.GAP_X) * (sizeOf(k).h + T.GAP_Y), 0);
            const widest = Math.max(...keys.map(k => sizeOf(k).w));
            return Math.max(widest, Math.sqrt(area * aspect));
        };
        const rowWidth = row => row.reduce((s, k, i) => s + sizeOf(k).w + (i ? T.GAP_X : 0), 0);
        const rowHeight = row => Math.max(...row.map(k => sizeOf(k).h));
        // The row width that makes this block biggest once the map is fitted
        // to the screen: try every place a row could break, keep the one
        // whose width/height binds least. Estimating from area alone came
        // out much too tall, because a row is as tall as its tallest member.
        const bestRows = (keys, extraW, extraH) => {
            let best = null;
            let total = 0;
            const candidates = new Set();
            for (const k of keys) { total += sizeOf(k).w + T.GAP_X; candidates.add(total); }
            candidates.add(Math.max(...keys.map(k => sizeOf(k).w)));
            for (const limit of candidates) {
                const rows = rowsOf(keys, limit);
                const w = extraW + Math.max(...rows.map(rowWidth));
                const h = extraH + rows.reduce((s, r) => s + T.GAP_Y + rowHeight(r), 0);
                const cost = Math.max(w / screen, h);
                if (!best || cost < best.cost) best = { rows, cost };
            }
            return best.rows;
        };

        // Sizes, bottom up
        const measure = id => {
            const kids = children.get(id);
            kids.forEach(measure);
            if (!kids.length) { blocks.set(id, { w: T.W, h: T.H }); return; }
            const ends = kids.filter(k => !children.get(k).length);
            // Subtrees tallest first: a row is as tall as its tallest member,
            // so like sizes together waste the least space
            const inner = kids.filter(k => children.get(k).length)
                .sort((a, b) => blocks.get(b).h - blocks.get(a).h);
            const keys = [];
            const columns = Math.ceil(ends.length / MAX_STACK);
            for (let c = 0; c < columns; c++) {
                const ids = ends.slice(c * Math.ceil(ends.length / columns), (c + 1) * Math.ceil(ends.length / columns));
                if (!ids.length) continue;
                const key = `col:${id}:${c}`;
                items.set(key, { w: T.W, h: ids.length * T.H + (ids.length - 1) * STACK, ids });
                keys.push(key);
            }
            keys.push(...inner);
            const rows = bestRows(keys, T.INDENT, T.H);
            const w = Math.max(T.W, T.INDENT + Math.max(...rows.map(rowWidth)));
            const h = T.H + rows.reduce((s, r) => s + T.GAP_Y + rowHeight(r), 0);
            blocks.set(id, { w, h, rows });
        };
        // Each block is shaped on its own, which is not the same as the best
        // shape for the whole map. So lay it out for a range of target shapes
        // and keep the one that comes out biggest when fitted to the screen.
        const PAD = 160;
        let bestFactor = 1, bestScale = -1;
        for (const factor of [0.35, 0.5, 0.7, 1, 1.4, 2, 2.8]) {
            screen = screenAspect * factor;
            blocks.clear();
            items.clear();
            roots.forEach(measure);
            const rows = bestRows(roots, 0, 0);
            const w = Math.max(...rows.map(rowWidth)) + T.GAP_X * 2 * (Math.max(...rows.map(r => r.length)) - 1);
            const h = rows.reduce((s, r) => s + rowHeight(r) + T.GAP_Y * 2, 0);
            const scale = Math.min(this.width / (w + PAD), this.height / (h + PAD));
            if (scale > bestScale) { bestScale = scale; bestFactor = factor; }
        }
        screen = screenAspect * bestFactor;
        blocks.clear();
        items.clear();
        roots.forEach(measure);

        // Positions, top down
        const buses = [];
        const entry = new Map();   // child id -> y its own link starts at (on its bus or column)
        const busOf = new Map();   // child id -> y of the bus its row hangs from
        const place = (id, bx, by) => {
            const node = byId.get(id);
            node.x = bx + T.W / 2;
            node.y = by + T.CY;
            node.fx = node.x;
            node.fy = node.y;
            const block = blocks.get(id);
            if (!block.rows) return;
            const trunkX = bx + T.TRUNK;
            let top = by + T.H;
            let lastBus = null;
            for (const row of block.rows) {
                const busY = top + T.GAP_Y / 2;
                const rowTop = top + T.GAP_Y;
                let x = bx + T.INDENT;
                let lastX = x;
                for (const key of row) {
                    const item = items.get(key);
                    if (item) {
                        // a column of end devices on one vertical line
                        let y = rowTop;
                        let from = busY;
                        for (const leaf of item.ids) {
                            place(leaf, x, y);
                            entry.set(leaf, from);
                            busOf.set(leaf, busY);
                            from = byId.get(leaf).y - 12;
                            y += T.H + STACK;
                        }
                        buses.push(`M${x + T.TRUNK},${busY} V${from}`);
                    } else {
                        place(key, x, rowTop);
                        entry.set(key, busY);
                        busOf.set(key, busY);
                    }
                    lastX = x;
                    x += sizeOf(key).w + T.GAP_X;
                }
                buses.push(`M${trunkX},${busY} H${lastX + T.TRUNK}`);
                lastBus = busY;
                top = rowTop + rowHeight(row);
            }
            // The trunk leaves the parent from the left of its icon
            buses.push(`M${node.x - 29},${node.y + 12} H${trunkX} V${lastBus}`);
        };
        const top = bestRows(roots, 0, 0);
        let y = 0;
        for (const row of top) {
            let x = 0;
            for (const id of row) { place(id, x, y); x += blocks.get(id).w + T.GAP_X * 3; }
            y += rowHeight(row) + T.GAP_Y * 2;
        }

        // Each tree link is drawn from the bus down into its device's left
        // side - the part that belongs to it alone, so its colour is its own
        for (const [childId, link] of uplink) {
            const child = byId.get(childId);
            const x = child.x - T.W / 2 + T.TRUNK;
            const from = entry.get(childId);
            // Only the part of the line that is this link's alone: from its
            // bus (or the device above it in a column) into the device
            link.route = `M${x},${from} V${child.y - 12} H${child.x - 29}`;
            link.labelX = x + 6;
            link.labelY = child.y - 12 - 6;
            link.inTree = true;
            // The whole way from the parent, along the trunk and the bus:
            // what the focus draws, when the shared lines are faded out
            const par = byId.get(endId(link.source) === childId ? endId(link.target) : endId(link.source));
            const trunkX = par.x - T.W / 2 + T.TRUNK;
            link.fullRoute = `M${par.x - 29},${par.y + 12} H${trunkX} V${busOf.get(childId)} H${x} V${child.y - 12} H${child.x - 29}`;
        }
        for (const link of this.links) {
            if (treeLinks.has(link)) continue;
            link.inTree = false;
            link.route = null;
            link.fullRoute = null;
        }
        this.treeBuses = buses;
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

    // -- Focus ---------------------------------------------------------------
    //
    // One device (or the devices in trouble) and only what matters for it:
    // the way to what it depends on - an access point's controller, a
    // camera's recorder, otherwise the core - and the devices hanging below
    // it, which lose service with it. Everything else fades out, and the
    // view zooms to the path. Clearing it fits the whole map again.

    focusTargetType(node) {
        const type = this.getDeviceType(node);
        if (type === 'ap') return 'wlc';
        if (type === 'camera') return 'nvr';
        return 'core';
    }

    computeFocus(ids) {
        const { byId, children, uplink } = this.buildTree();
        const endId = e => (typeof e === 'object' ? e.id : e);
        const treeLinks = new Set(uplink.values());
        const adjacency = new Map(this.nodes.map(n => [n.id, []]));
        for (const link of this.links) {
            const a = endId(link.source), b = endId(link.target);
            if (!adjacency.has(a) || !adjacency.has(b)) continue;
            // a tree link is the expected way; a second uplink only when shorter
            const w = treeLinks.has(link) ? 1 : 1.5;
            adjacency.get(a).push([b, link, w]);
            adjacency.get(b).push([a, link, w]);
        }
        const parentOf = new Map();
        for (const [p, kids] of children) for (const k of kids) parentOf.set(k, p);
        const linkTo = new Map();
        for (const [child, link] of uplink) linkTo.set(child, link);

        const keep = new Set(), impact = new Set(), keepLinks = new Set(), impactLinks = new Set();
        const paths = [];
        for (const id of ids) {
            const node = byId.get(id);
            if (!node) continue;
            const want = this.focusTargetType(node);
            // Cheapest way to the nearest device of the wanted type
            const dist = new Map([[id, 0]]);
            const prev = new Map();
            const done = new Set();
            let found = null;
            for (;;) {
                let cur = null, best = Infinity;
                for (const [k, d] of dist) if (!done.has(k) && d < best) { best = d; cur = k; }
                if (cur === null) break;
                done.add(cur);
                if (this.getDeviceType(byId.get(cur)) === want) { found = cur; break; }
                for (const [other, link, w] of adjacency.get(cur)) {
                    if (done.has(other)) continue;
                    if (best + w < (dist.get(other) ?? Infinity)) {
                        dist.set(other, best + w);
                        prev.set(other, [cur, link]);
                    }
                }
            }
            const chain = [];
            if (found !== null) {
                for (let cur = found; cur !== undefined; cur = prev.get(cur)?.[0]) {
                    chain.unshift(cur);
                    const step = prev.get(cur);
                    if (step) keepLinks.add(step[1]);
                }
            } else {
                // Nothing of that type on this map: up the tree to its root
                for (let cur = id; cur !== undefined; cur = parentOf.get(cur)) {
                    chain.push(cur);
                    if (linkTo.has(cur)) keepLinks.add(linkTo.get(cur));
                }
            }
            chain.forEach(c => keep.add(c));
            paths.push({ id, target: want, found: found !== null, chain: found !== null ? chain : chain.slice() });
            // Everything that hangs below it
            const stack = [...(children.get(id) || [])];
            while (stack.length) {
                const cur = stack.pop();
                impact.add(cur);
                if (linkTo.has(cur)) impactLinks.add(linkTo.get(cur));
                stack.push(...(children.get(cur) || []));
            }
        }
        for (const k of keep) impact.delete(k);
        return { ids: ids.filter(i => byId.has(i)), keep, impact, keepLinks, impactLinks, paths, byId };
    }

    // ids: the devices to focus; zoom: frame the path
    focusOn(ids, { zoom = true } = {}) {
        this.focusIds = (ids || []).map(String);
        this.refreshFocus();
        if (zoom && this.focus) this.zoomToFocus();
    }

    clearFocus({ fit = true } = {}) {
        const had = this.focusIds !== null;
        this.focusIds = null;
        this.refreshFocus();
        if (fit && had) this.fitView();
    }

    // Recompute after a render (the refresh brings new node objects) and
    // repaint; a focused device that left the map drops out of the focus
    refreshFocus() {
        const before = this.focus ? this.focus.ids.join(',') : '';
        this.focus = this.focusIds && this.nodes.length ? this.computeFocus(this.focusIds) : null;
        if (this.focus && !this.focus.ids.length) this.focus = null;
        const on = !!this.focus;
        const f = this.focus;
        this.svg.classed('focus-mode', on);
        this.nodesGroup.selectAll('g.node')
            .classed('focus-keep', d => on && f.keep.has(d.id))
            .classed('focus-origin', d => on && f.ids.includes(d.id))
            .classed('focus-impact', d => on && f.impact.has(d.id));
        this.linksGroup.selectAll('g.link-group')
            .classed('focus-keep', d => on && f.keepLinks.has(d))
            .classed('focus-impact', d => on && f.impactLinks.has(d));
        this.drawFocusRoutes();
        const after = on ? f.ids.join(',') : '';
        if (this.onFocusChange && (before !== after || on)) this.onFocusChange(this.focusInfo());
    }

    drawFocusRoutes() {
        const f = this.focus;
        const endOf = e => (typeof e === 'object' ? e : f.byId.get(e));
        const routeOf = l => (this.layoutMode === 'tree' && l.fullRoute) ? l.fullRoute
            : `M${endOf(l.source).x},${endOf(l.source).y} L${endOf(l.target).x},${endOf(l.target).y}`;
        const data = f ? [...f.impactLinks].map(l => ({ l, cls: 'impact' }))
            .concat([...f.keepLinks].map(l => ({ l, cls: 'path' }))) : [];
        this.focusGroup.selectAll('path').data(data).join('path')
            .attr('class', d => `focus-route ${d.cls} status-${d.l.status}`)
            .attr('d', d => routeOf(d.l));
    }

    // What the page shows above the map: each focused device's path, named
    focusInfo() {
        const f = this.focus;
        if (!f) return null;
        const view = id => {
            const n = f.byId.get(id);
            return { id, hostname: n.hostname, type: this.getDeviceType(n), status: n.status };
        };
        return {
            ids: f.ids,
            impactCount: f.impact.size,
            paths: f.paths.map(p => ({ ...p, chain: p.chain.map(view) })),
        };
    }

    zoomToFocus() {
        const f = this.focus;
        if (!f) return;
        this.measure();
        // the devices that lose service too, while there are few enough of
        // them that the path stays big
        const ids = f.impact.size <= 12 ? [...f.keep, ...f.impact] : [...f.keep];
        const pts = ids.map(id => f.byId.get(id)).filter(n => n && n.x != null);
        if (!pts.length) return;
        const xs = pts.map(n => n.x), ys = pts.map(n => n.y);
        // a device's box around its centre: icon, name and address
        const x0 = Math.min(...xs) - 90, x1 = Math.max(...xs) + 90;
        const y0 = Math.min(...ys) - 60, y1 = Math.max(...ys) + 90;
        // What the page lays over the map (the focus bar, the side panel)
        // is not room to draw in
        const inset = Object.assign({ top: 0, right: 0, bottom: 0, left: 0 },
            this.getInsets ? this.getInsets() : {});
        const w = Math.max(100, this.width - inset.left - inset.right);
        const h = Math.max(100, this.height - inset.top - inset.bottom);
        const scale = Math.max(0.1, Math.min(1.6, w / (x1 - x0), h / (y1 - y0)));
        this.autoFit = false;
        this.svg.transition().duration(750).call(
            this.zoom.transform,
            d3.zoomIdentity
                .translate(inset.left + w / 2, inset.top + h / 2)
                .scale(scale)
                .translate(-(x0 + x1) / 2, -(y0 + y1) / 2)
        );
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
