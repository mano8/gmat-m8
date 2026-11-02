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

  // A print button (the purchase-order page): without JavaScript the browser's own print
  // command does the same.
  document.addEventListener("click", function (event) {
    if (event.target.closest("[data-print]")) {
      window.print();
    }
  });

  // A form marked data-once disables its buttons once sent, so a double tap posts once. The
  // idempotency key in the form is the real guard; this only spares the second request. A page
  // restored from the back/forward cache enables them again.
  document.addEventListener("submit", function (event) {
    var form = event.target;
    if (!form.hasAttribute || !form.hasAttribute("data-once")) {
      return;
    }
    window.setTimeout(function () {
      form.querySelectorAll("button[type=submit]").forEach(function (button) {
        button.disabled = true;
      });
    }, 0);
  });
  window.addEventListener("pageshow", function (event) {
    if (event.persisted) {
      document.querySelectorAll("form[data-once] button[type=submit]").forEach(function (button) {
        button.disabled = false;
      });
    }
  });

  // A list refreshed through htmx: the toolbar's live region announces the new result count
  // (the count itself sits in the swapped results, which a screen reader would not announce).
  document.addEventListener("htmx:afterSettle", function () {
    var status = document.querySelector("[data-list-status]");
    var count = document.querySelector("[data-list-count]");
    if (status && count) {
      status.textContent = count.textContent.trim();
    }
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
