---
name: frontend-aesthetics
description: >-
  Produce polished, accessible frontend UI with design tokens, semantic
  HTML, and Pico CSS. Use when creating HTML templates, adding CSS,
  building user-facing pages, or working with Jinja2 templates.
  Covers design tokens, typography, components, accessibility (WCAG 2.1 AA),
  dark mode, and micro-interactions.
  Do NOT use for backend API design or data modeling.
metadata:
  author: Geoff
  version: 1.0.0
---

# Frontend Aesthetics

The difference between "works" and "wow" is 20 lines of CSS and semantic HTML choices.

## Instructions

### Step 1: Follow the Core Philosophy

1. **Design with tokens, not magic numbers** - every value from a system
2. **Semantic HTML is the foundation** - right element gets 80% of the way
3. **Accessibility is not optional** - WCAG 2.1 AA minimum
4. **Motion is meaning** - animate to communicate; respect `prefers-reduced-motion`
5. **Dark mode is table stakes** - use CSS custom properties

### Step 2: Use the Design Token System

See `references/design-tokens.md` for the complete token system including colors, spacing, typography, shadows, and dark mode adaptation.

Key rules:
- Never use raw hex codes or pixel values in component styles
- All colors from tokens or Pico defaults
- All spacing uses `--space-*` tokens (8px rhythm)
- Typography uses `clamp()` for fluid scaling

### Step 3: Build with Component Patterns

See `references/component-patterns.md` for cards, badges, forms, alerts, and layout patterns.

### Step 4: Ensure Accessibility (Non-Negotiable)

- **Color contrast**: 4.5:1 for body text, 3:1 for large text
- **Focus management**: Visible `:focus-visible` ring on all interactive elements
- **Motion safety**: `prefers-reduced-motion` rule in every template
- **Semantic HTML**: `<nav>`, `<article>`, `<section>`, `role="alert"` for errors
- **Touch targets**: Minimum 44px (2.75rem)

### Step 5: Use the Pre-Ship Checklist

- [ ] All colors from tokens or Pico defaults
- [ ] All spacing uses `--space-*` tokens
- [ ] Page has exactly one `<h1>`; heading levels don't skip
- [ ] All interactive elements have `:focus-visible` states
- [ ] `prefers-reduced-motion` is respected
- [ ] `role="alert"` on error messages
- [ ] No text wider than ~65ch
- [ ] Touch targets >= 44px
- [ ] Dark mode works (toggle `data-theme`)

## Examples

### Example 1: Jinja2 Card Grid with Conditional Badges

```html
<div class="card-grid">
    {% for item in items %}
    <article class="card {{ 'card-highlighted' if item.featured else '' }}">
        <h3>{{ item.title }}</h3>
        <p>{{ item.description }}</p>
        {% if item.severity == 'high' %}
            <span class="badge badge-danger">High</span>
        {% elif item.severity == 'medium' %}
            <span class="badge badge-warning">Medium</span>
        {% endif %}
    </article>
    {% endfor %}
</div>
```

### Example 2: Accessible Error Display

```html
{% if error %}
<div class="alert alert-error" role="alert" aria-live="assertive">
    <strong>Error:</strong> {{ error }}
</div>
{% endif %}
```

## Troubleshooting

### Error: Colors look wrong in dark mode
- Verify you're using CSS custom properties, not hardcoded colors
- Check that dark mode tokens override light mode values
- Test with both `prefers-color-scheme: dark` and `data-theme="dark"`

### Error: Layout breaks on mobile
- Use `min(72rem, 100% - var(--space-xl) * 2)` for containers
- Use `100dvh` instead of `100vh` for full-height layouts
- Use `minmax(min(250px, 100%), 1fr)` for grid columns
