/**
 * Theme management - applies saved theme on page load
 * Include this script in all pages to ensure consistent theming
 *
 * Two themes: "dark" (stealth obsidian) and "light" (tactical chalk). Any
 * other saved value - such as "hud" from the preview builds - falls back to
 * dark, so nobody is left on a theme that no longer exists.
 */

(function () {
    // Apply saved theme immediately to prevent flash
    let savedTheme = null;
    try { savedTheme = localStorage.getItem('theme'); } catch (e) { /* storage blocked */ }
    const theme = savedTheme === 'light' ? 'light' : 'dark';
    document.documentElement.setAttribute('data-theme', theme);
})();
