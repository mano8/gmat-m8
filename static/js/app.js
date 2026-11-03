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
  // A page with several lists wraps each one (toolbar and results) in a [data-list-section].
  document.addEventListener("htmx:afterSettle", function (event) {
    // Only a swap that brings a result count (not a delivery badge polling every few seconds).
    var swapped = event.target;
    if (!(swapped.matches && (swapped.matches("[data-list-count]") || swapped.querySelector("[data-list-count]")))) {
      return;
    }
    var scope = (event.target.closest && event.target.closest("[data-list-section]")) || document;
    var status = scope.querySelector("[data-list-status]");
    var count = scope.querySelector("[data-list-count]");
    if (status && count) {
      status.textContent = count.textContent.trim();
    }
  });

  // A badge that reloads itself (data-poll, the email delivery status): only a 200 from the
  // URL it asked may replace it. A redirect (the session ended or needs MFA again: the login
  // page would land inside the badge) or an error keeps the badge as it is and stops the
  // polling, which htmx does once the element has left the page.
  document.addEventListener("htmx:beforeSwap", function (event) {
    var element = event.detail.elt;
    if (!element.hasAttribute || !element.hasAttribute("data-poll")) {
      return;
    }
    var xhr = event.detail.xhr;
    var asked = new URL(event.detail.requestConfig.path, window.location.href).pathname;
    var answered = xhr.responseURL ? new URL(xhr.responseURL).pathname : asked;
    if (xhr.status === 200 && answered === asked) {
      return;
    }
    event.detail.shouldSwap = false;
    var still = element.cloneNode(true);
    ["hx-get", "hx-trigger", "hx-swap", "data-poll"].forEach(function (name) {
      still.removeAttribute(name);
    });
    element.replaceWith(still);
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
