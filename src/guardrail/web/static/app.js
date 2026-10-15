// Reload every minute and keep the scroll position. The page is complete
// without this file.
(function () {
  var key = "gr-scroll:" + location.pathname + location.search;
  try {
    var saved = sessionStorage.getItem(key);
    if (saved) { window.scrollTo(0, Number(saved)); }
  } catch (e) { /* storage unavailable */ }
  setTimeout(function () {
    try { sessionStorage.setItem(key, String(window.scrollY)); } catch (e) { /* ignore */ }
    location.reload();
  }, 60000);
})();
