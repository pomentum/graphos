// The light/dark switch, as in the graphos whitepaper: dark until the reader picks;
// the pick is kept in localStorage where the browser allows it.
(function(){
  var toc = document.getElementById('toc');
  if (toc) {
    toc.addEventListener('click', function(e){ if (e.target.closest('a')) toc.open = false; });
    document.addEventListener('keydown', function(e){ if (e.key === 'Escape' && toc.open){ toc.open = false; toc.querySelector('summary').focus(); } });
    document.addEventListener('click', function(e){ if (toc.open && !toc.contains(e.target)) toc.open = false; });
  }
  // The page opens dark until the reader picks a theme; the pick is remembered where the
  // browser allows it, and the page works the same where it does not.
  var root = document.documentElement, dark = matchMedia('(prefers-color-scheme: dark)');
  var buttons = document.querySelectorAll('.mode button');
  function show(){
    var now = root.dataset.theme || (dark.matches ? 'dark' : 'light');
    buttons.forEach(function(b){ b.setAttribute('aria-pressed', String(b.dataset.set === now)); });
  }
  buttons.forEach(function(b){ b.addEventListener('click', function(){
    root.dataset.theme = b.dataset.set;
    try { localStorage.setItem('graphos-theme', b.dataset.set); } catch (e) {}
    show();
  }); });
  if (dark.addEventListener) dark.addEventListener('change', show);
  show();
})();
