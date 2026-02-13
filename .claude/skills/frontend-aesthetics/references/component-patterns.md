# Component Patterns

## Cards

```css
.card {
    background: var(--surface-1);
    border: 1px solid var(--surface-3);
    border-radius: var(--radius-md);
    padding: var(--space-lg);
    box-shadow: var(--shadow-sm);
    transition: box-shadow var(--duration-normal) var(--ease-out),
                transform var(--duration-normal) var(--ease-out);
}
.card:hover {
    box-shadow: var(--shadow-md);
    transform: translateY(-2px);
}
@media (prefers-reduced-motion: reduce) {
    .card:hover { transform: none; }
}

.card-grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(min(250px, 100%), 1fr));
    gap: var(--space-lg);
}
```

## Badges / Tags

```css
.badge {
    display: inline-flex;
    align-items: center;
    gap: var(--space-xs);
    padding: var(--space-xs) var(--space-sm);
    font-size: var(--text-xs);
    font-weight: 600;
    line-height: 1;
    border-radius: var(--radius-full);
    white-space: nowrap;
}
.badge-danger  { background: hsl(0 72% 51% / 0.12);   color: hsl(0 72% 40%); }
.badge-warning { background: hsl(38 92% 50% / 0.12);   color: hsl(38 70% 35%); }
.badge-success { background: hsl(145 63% 42% / 0.12);  color: hsl(145 63% 30%); }
.badge-info    { background: hsl(200 80% 50% / 0.12);  color: hsl(200 80% 35%); }
```

## Forms

```css
input, select, textarea {
    border: 1.5px solid var(--surface-3);
    border-radius: var(--radius-md);
    padding: var(--space-sm) var(--space-md);
    font-size: var(--text-base);
    background: var(--surface-1);
    color: var(--text-1);
    transition: border-color var(--duration-fast) var(--ease-out),
                box-shadow var(--duration-fast) var(--ease-out);
    min-height: 2.75rem;  /* 44px touch target */
}
input:focus-visible, select:focus-visible, textarea:focus-visible {
    outline: none;
    border-color: var(--brand);
    box-shadow: var(--shadow-glow);
}
button, [type="submit"] {
    min-height: 2.75rem;
    padding: var(--space-sm) var(--space-xl);
    font-weight: 600;
    border-radius: var(--radius-md);
    cursor: pointer;
}
```

## Alerts

```css
.alert {
    padding: var(--space-md) var(--space-lg);
    border-radius: var(--radius-md);
    border-left: 4px solid;
}
.alert-error   { background: hsl(0 72% 51% / 0.08); border-color: var(--color-danger); }
.alert-success { background: hsl(145 63% 42% / 0.08); border-color: var(--color-success); }
.alert-info    { background: hsl(200 80% 50% / 0.08); border-color: var(--color-info); }
```

## Layout

```css
body { min-height: 100dvh; display: flex; flex-direction: column; background: var(--surface-2); }
main { flex: 1; }
footer { margin-top: auto; }

.container { width: min(72rem, 100% - var(--space-xl) * 2); margin-inline: auto; }
```

## Accessibility Essentials

```css
:focus-visible { outline: 2px solid var(--brand); outline-offset: 2px; }
:focus:not(:focus-visible) { outline: none; }

@media (prefers-reduced-motion: reduce) {
    *, *::before, *::after {
        animation-duration: 0.01ms !important;
        transition-duration: 0.01ms !important;
    }
}

.sr-only {
    position: absolute; width: 1px; height: 1px;
    padding: 0; margin: -1px; overflow: hidden;
    clip: rect(0,0,0,0); white-space: nowrap; border: 0;
}
```

## Micro-Interactions

```css
/* Page entrance */
main { animation: fadeUp var(--duration-normal) var(--ease-out); }
@keyframes fadeUp { from { opacity: 0; transform: translateY(8px); } }

/* Skeleton loading */
.skeleton-line {
    height: 1em;
    border-radius: var(--radius-sm);
    background: linear-gradient(90deg, var(--surface-3) 25%, var(--surface-2) 50%, var(--surface-3) 75%);
    background-size: 200% 100%;
    animation: shimmer 1.5s ease-in-out infinite;
}
@keyframes shimmer { from { background-position: 200% 0; } to { background-position: -200% 0; } }

/* Staggered card entrance */
.card-grid > :nth-child(1) { animation-delay: 0ms; }
.card-grid > :nth-child(2) { animation-delay: 50ms; }
.card-grid > :nth-child(3) { animation-delay: 100ms; }
.card-grid > * { animation: fadeUp var(--duration-normal) var(--ease-out) backwards; }
```

## Semantic HTML Quick Reference

| Need | Use | NOT |
|------|-----|-----|
| Navigation | `<nav>` | `<div class="nav">` |
| Page section | `<section>` with heading | `<div>` |
| Self-contained | `<article>` | `<div class="card">` |
| Error alert | `<div role="alert">` | `<div class="error">` |
| Loading | `aria-busy="true"` | No indication |
| Icon button | `<button aria-label="Close">` | `<button>X</button>` |
