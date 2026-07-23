(function () {
  var root = document.documentElement;
  var btns = document.querySelectorAll('.theme-toggle');

  function current() {
    return root.getAttribute('data-theme') === 'dark' ? 'dark' : 'light';
  }

  function label() {
    return current() === 'dark' ? '☀ Light' : '☾ Dark';
  }

  function render() {
    btns.forEach(function (b) {
      b.textContent = label();
    });
  }

  btns.forEach(function (b) {
    b.addEventListener('click', function () {
      var next = current() === 'dark' ? 'light' : 'dark';
      root.setAttribute('data-theme', next);
      try {
        localStorage.setItem('theme', next);
      } catch (e) { /* quota exceeded, etc. – silently ignore */ }
      render();
    });
  });

  render();
})();
