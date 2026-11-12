"use strict";
/* The cockpit's themes, kept in this browser. The stylesheet holds the palettes:
   data-theme on <html> is the only switch. The default is indigo, so a missing
   or unreadable preference never changes the page. */
const COCKPIT_THEME_KEY = 'cockpit.theme';
const COCKPIT_THEME_COLOR = Object.freeze({indigo: '#170b38', graphite: '#09090b', paper: '#f6f7fb'});
const cockpitTheme = name => Object.prototype.hasOwnProperty.call(COCKPIT_THEME_COLOR, name);

function themeName(){
  const name = document.documentElement.dataset.theme;
  return cockpitTheme(name) ? name : 'indigo';
}

function applyTheme(name, persist){
  name = cockpitTheme(name) ? name : 'indigo';
  document.documentElement.dataset.theme = name;
  const meta = document.querySelector('meta[name="theme-color"]');
  if (meta) meta.setAttribute('content', COCKPIT_THEME_COLOR[name]);
  if (persist !== false){
    try { localStorage.setItem(COCKPIT_THEME_KEY, name); } catch { /* private mode keeps it for this page */ }
  }
  if (window.cockpitBackdrop) window.cockpitBackdrop.sync();
  return name;
}

(function(){
  let saved = null;
  try { saved = localStorage.getItem(COCKPIT_THEME_KEY); } catch { /* private mode: the default */ }
  applyTheme(saved, false);
})();
