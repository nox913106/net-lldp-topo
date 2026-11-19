/**
 * The login page: sign in, the "waiting for a role" screen, and changing
 * the password (forced on first login, or chosen from the account menu).
 */
(function () {
    const $ = id => document.getElementById(id);
    const params = new URLSearchParams(location.search);

    // Only ever go back to a path on this site: "/devices", not
    // "//evil.example" or an absolute URL (an open redirect would make the
    // login page a phishing tool)
    function nextUrl() {
        const next = params.get('next') || '/';
        return /^\/(?!\/)[^\s\\]*$/.test(next) && !next.startsWith('/login') ? next : '/';
    }

    function show(id) {
        ['login-form', 'login-pending', 'change-form'].forEach(x => { $(x).hidden = x !== id; });
        const first = $(id).querySelector('input');
        if (first) first.focus();
    }

    function error(id, message) {
        $(id).textContent = message || '';
        $(id).hidden = !message;
    }

    async function json(url, body) {
        const r = await fetch(url, {
            method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body)
        });
        const data = await r.json().catch(() => ({}));
        if (!r.ok) throw new Error(typeof data.detail === 'string' ? data.detail : `HTTP ${r.status}`);
        return data;
    }

    function route(me) {
        if (me.role === 'guest') {
            $('pending-who').textContent = `${me.display_name || me.username} (${me.username})`;
            show('login-pending');
        } else if (me.must_change_password) {
            $('change-cancel').hidden = true;
            show('change-form');
        } else if (params.get('change') === '1' && me.source === 'local') {
            $('change-why').hidden = true;
            $('change-cancel').hidden = false;
            show('change-form');
        } else {
            location.replace(nextUrl());
        }
    }

    $('login-form').addEventListener('submit', async e => {
        e.preventDefault();
        error('login-error', '');
        const button = $('login-submit');
        button.disabled = true;
        button.textContent = i18n.t('signingIn');
        try {
            const me = await json('/api/v1/auth/login', {
                username: $('login-username').value.trim(),
                password: $('login-password').value
            });
            $('login-password').value = '';
            $('change-current').value = '';
            route(me);
        } catch (err) {
            error('login-error', err.message);
            $('login-password').select();
        } finally {
            button.disabled = false;
            button.textContent = i18n.t('signIn');
        }
    });

    $('change-form').addEventListener('submit', async e => {
        e.preventDefault();
        error('change-error', '');
        if ($('change-new').value !== $('change-repeat').value) {
            error('change-error', i18n.t('passwordsDiffer'));
            return;
        }
        try {
            await json('/api/v1/auth/password', {
                current_password: $('change-current').value,
                new_password: $('change-new').value
            });
            location.replace(nextUrl());
        } catch (err) {
            error('change-error', err.message);
        }
    });

    $('change-cancel').addEventListener('click', () => history.length > 1 ? history.back() : location.replace('/'));

    $('pending-logout').addEventListener('click', async () => {
        await fetch('/api/v1/auth/logout', { method: 'POST' }).catch(() => null);
        location.replace('/login');
    });

    document.querySelectorAll('.login-lang [data-lang]').forEach(b =>
        b.addEventListener('click', () => i18n.setLanguage(b.dataset.lang)));

    document.addEventListener('DOMContentLoaded', async () => {
        i18n.init();
        // Already logged in? Then this page is the pending / change screen
        try {
            const r = await fetch('/api/v1/auth/me');
            if (r.ok) {
                route(await r.json());
                return;
            }
        } catch (e) { /* not logged in */ }
        try {
            const info = await (await fetch('/api/v1/auth/info')).json();
            if (info.needs_setup) error('login-error', i18n.t('needsSetup'));
            if (info.ldap_enabled) {
                $('login-hint').hidden = false;
                if (info.login_hint) {
                    $('login-suffix').textContent = '@' + info.login_hint.replace(/^@/, '');
                    $('login-suffix').hidden = false;
                }
            }
        } catch (e) { /* the form works without it */ }
        show('login-form');
    });
})();
