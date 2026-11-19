/**
 * Who is logged in, on every page: the account menu in the header, the
 * Admin tab for administrators, and a role class on <body> so a page can
 * hide what this role cannot do (the server refuses it either way:
 * app/core/security_middleware.py).
 */
(function () {
    const T = key => (window.i18n ? window.i18n.t(key) : key);
    const ROLE_KEY = { admin: 'roleAdmin', operator: 'roleOperator', viewer: 'roleViewer', guest: 'roleGuest' };

    function render(me) {
        document.body.classList.add('role-' + me.role);
        window.currentUser = me;
        const header = document.querySelector('.header');
        if (!header) return;

        const nav = header.querySelector('.nav');
        if (nav && me.role === 'admin' && !nav.querySelector('[href="/admin"]')) {
            const a = document.createElement('a');
            a.href = '/admin';
            a.className = 'nav-item' + (location.pathname === '/admin' ? ' active' : '');
            a.setAttribute('data-i18n', 'backOffice');
            a.textContent = T('backOffice');
            nav.appendChild(a);
        }

        let menu = header.querySelector('.user-menu');
        if (!menu) {
            menu = document.createElement('div');
            menu.className = 'user-menu';
            header.appendChild(menu);
        }
        const name = escapeHtml(me.display_name || me.username);
        menu.innerHTML = `
            <button type="button" class="user-menu-button" aria-haspopup="true" aria-expanded="false">
                <span class="user-menu-name">${name}</span>
                <span class="user-menu-role" data-i18n="${ROLE_KEY[me.role] || ''}">${escapeHtml(T(ROLE_KEY[me.role] || me.role))}</span>
            </button>
            <div class="user-menu-list" hidden>
                <div class="user-menu-who">${escapeHtml(me.username)} · ${escapeHtml(T(me.source === 'ldap' ? 'sourceLdap' : 'sourceLocal'))}</div>
                ${me.source === 'local' ? `<a href="/login?change=1" data-i18n="changePassword">${escapeHtml(T('changePassword'))}</a>` : ''}
                ${me.role === 'admin' ? `<a href="/admin" data-i18n="backOffice">${escapeHtml(T('backOffice'))}</a>` : ''}
                <button type="button" class="user-menu-logout" data-i18n="logout">${escapeHtml(T('logout'))}</button>
            </div>`;
        const button = menu.querySelector('.user-menu-button');
        const list = menu.querySelector('.user-menu-list');
        button.addEventListener('click', e => {
            e.stopPropagation();
            list.hidden = !list.hidden;
            button.setAttribute('aria-expanded', String(!list.hidden));
        });
        document.addEventListener('click', () => {
            list.hidden = true;
            button.setAttribute('aria-expanded', 'false');
        });
        menu.querySelector('.user-menu-logout').addEventListener('click', async () => {
            try {
                await fetch('/api/v1/auth/logout', { method: 'POST' });
            } finally {
                location.href = '/login';
            }
        });
    }

    async function load() {
        try {
            const response = await fetch('/api/v1/auth/me');
            if (!response.ok) return;
            render(await response.json());
        } catch (e) { /* offline: the page shows its own error */ }
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', load);
    } else {
        load();
    }
})();
