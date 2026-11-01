/**
 * browser/dom_utils.js
 * --------------------
 * Unified DOM inspection & label resolution helper functions for Playwright.
 */

window.__ai_dom_utils = {
    /**
     * Infer human-readable label for a DOM element across 7 priority sources.
     */
    inferLabel: function(el) {
        if (!el) return "";

        // 1. Explicit <label for="id">
        if (el.id) {
            const lbl = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
            if (lbl && lbl.innerText.trim()) return lbl.innerText.trim();
        }

        // 2. aria-label attribute
        const ariaLabel = el.getAttribute('aria-label');
        if (ariaLabel && ariaLabel.trim()) return ariaLabel.trim();

        // 3. aria-labelledby attribute
        const ariaLabelledBy = el.getAttribute('aria-labelledby');
        if (ariaLabelledBy) {
            const lbl = document.getElementById(ariaLabelledBy);
            if (lbl && lbl.innerText.trim()) return lbl.innerText.trim();
        }

        // 4. Wrapping <label>
        const parentLabel = el.closest('label');
        if (parentLabel) {
            const clone = parentLabel.cloneNode(true);
            clone.querySelectorAll('input, select, textarea, button').forEach(c => c.remove());
            const txt = clone.innerText.trim();
            if (txt) return txt;
        }

        // 5. Preceding sibling <label>
        let prev = el.previousElementSibling;
        while (prev) {
            if (prev.tagName === 'LABEL' && prev.innerText.trim()) {
                return prev.innerText.trim();
            }
            prev = prev.previousElementSibling;
        }

        // 6. Placeholder / Title / Alt attribute
        const placeholder = el.getAttribute('placeholder') || el.getAttribute('title') || el.getAttribute('alt');
        if (placeholder && placeholder.trim()) return placeholder.trim();

        // 7. Value or innerText fallback for buttons
        if (el.tagName === 'BUTTON' || el.type === 'submit' || el.type === 'button') {
            return (el.innerText || el.value || '').trim();
        }

        return "";
    },

    /**
     * Generate a robust CSS selector for an element.
     */
    uniqueSelector: function(el) {
        if (el.id) return `#${CSS.escape(el.id)}`;
        if (el.getAttribute('name')) return `${el.tagName.toLowerCase()}[name="${CSS.escape(el.getAttribute('name'))}"]`;
        if (el.getAttribute('data-testid')) return `[data-testid="${CSS.escape(el.getAttribute('data-testid'))}"]`;
        
        let path = [];
        let cur = el;
        while (cur && cur.nodeType === Node.ELEMENT_NODE && cur.tagName !== 'BODY') {
            let selector = cur.tagName.toLowerCase();
            if (cur.className && typeof cur.className === 'string') {
                const classes = cur.className.trim().split(/\s+/).filter(c => !c.startsWith('ai-agent-'));
                if (classes.length > 0) {
                    selector += '.' + classes.slice(0, 2).map(CSS.escape).join('.');
                }
            }
            let sibling = cur;
            let nth = 1;
            while (sibling = sibling.previousElementSibling) {
                if (sibling.tagName === cur.tagName) nth++;
            }
            if (nth > 1) selector += `:nth-of-type(${nth})`;
            path.unshift(selector);
            cur = cur.parentElement;
        }
        return path.join(' > ');
    }
};
