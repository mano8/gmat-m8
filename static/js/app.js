// Small and CSP-safe: no inline handlers, no eval, nothing written to localStorage.
(function () {
  "use strict";

  // Toasts: the close button removes its message.
  document.addEventListener("click", function (event) {
    var button = event.target.closest("[data-dismiss]");
    if (!button) {
      return;
    }
    var target = button.closest(button.getAttribute("data-dismiss"));
    if (target) {
      target.remove();
    }
  });

  // Success messages fade after a few seconds.
  window.addEventListener("DOMContentLoaded", function () {
    document.querySelectorAll("[data-autohide]").forEach(function (node) {
      var delay = parseInt(node.getAttribute("data-autohide"), 10) || 6000;
      window.setTimeout(function () {
        node.remove();
      }, delay);
    });
  });

  // The language switcher submits when the selection changes (without JavaScript the form
  // still has its button).
  document.addEventListener("change", function (event) {
    var select = event.target.closest("select[data-autosubmit]");
    if (select && select.form) {
      select.form.requestSubmit();
    }
  });
})();
