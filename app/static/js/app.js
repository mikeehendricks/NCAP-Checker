/* Small progressive-enhancement helpers for NCAP Checker. */
(function () {
  'use strict';

  // Confirm before any destructive or slow action.
  document.addEventListener('click', function (event) {
    var target = event.target.closest('[data-confirm]');
    if (target && !window.confirm(target.getAttribute('data-confirm'))) {
      event.preventDefault();
      return false;
    }
  });

  // Disable a submit button after click so double-clicks do not fire two checks.
  document.addEventListener('submit', function (event) {
    var form = event.target;
    if (!form || form.dataset.guard === 'off') { return; }
    var button = form.querySelector('button[type="submit"], input[type="submit"]');
    if (!button || button.dataset.noSpinner === 'true') { return; }
    var original = button.innerHTML;
    button.disabled = true;
    button.dataset.originalHtml = original;
    window.setTimeout(function () {
      if (button && button.disabled) {
        button.disabled = false;
        if (button.dataset.originalHtml) { button.innerHTML = button.dataset.originalHtml; }
      }
    }, 20000);
  });

  // Auto-dismiss success alerts after a few seconds.
  window.setTimeout(function () {
    document.querySelectorAll('.alert-success').forEach(function (alert) {
      if (window.bootstrap && window.bootstrap.Alert) {
        var instance = window.bootstrap.Alert.getOrCreateInstance(alert);
        if (instance) { instance.close(); }
      }
    });
  }, 6000);
})();
