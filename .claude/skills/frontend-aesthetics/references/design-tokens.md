# Design Token System

## Color Tokens

```css
:root {
    /* Brand */
    --brand-hue: 220;
    --brand: hsl(var(--brand-hue) 70% 50%);
    --brand-light: hsl(var(--brand-hue) 70% 92%);
    --brand-dark: hsl(var(--brand-hue) 70% 30%);

    /* Semantic */
    --color-success: hsl(145 63% 42%);
    --color-warning: hsl(38 92% 50%);
    --color-danger: hsl(0 72% 51%);
    --color-info: hsl(200 80% 50%);

    /* Surface & Text (light mode) */
    --surface-1: hsl(0 0% 100%);
    --surface-2: hsl(0 0% 97%);
    --surface-3: hsl(0 0% 93%);
    --text-1: hsl(0 0% 10%);
    --text-2: hsl(0 0% 35%);
    --text-muted: hsl(0 0% 55%);

    /* Shadows */
    --shadow-sm: 0 1px 2px hsl(0 0% 0% / 0.05);
    --shadow-md: 0 4px 12px hsl(0 0% 0% / 0.08);
    --shadow-lg: 0 12px 32px hsl(0 0% 0% / 0.12);
    --shadow-glow: 0 0 0 3px hsl(var(--brand-hue) 70% 50% / 0.25);
}

/* Dark mode */
@media (prefers-color-scheme: dark) {
    :root:not([data-theme="light"]) {
        --surface-1: hsl(220 15% 12%);
        --surface-2: hsl(220 15% 16%);
        --surface-3: hsl(220 15% 21%);
        --text-1: hsl(0 0% 93%);
        --text-2: hsl(0 0% 72%);
        --text-muted: hsl(0 0% 50%);
        --brand-light: hsl(var(--brand-hue) 50% 20%);
        --shadow-sm: 0 1px 2px hsl(0 0% 0% / 0.2);
        --shadow-md: 0 4px 12px hsl(0 0% 0% / 0.3);
        --shadow-lg: 0 12px 32px hsl(0 0% 0% / 0.4);
    }
}
```

## Spacing Tokens (8px rhythm)

```css
:root {
    --space-xs: 0.25rem;   /* 4px */
    --space-sm: 0.5rem;    /* 8px */
    --space-md: 1rem;      /* 16px */
    --space-lg: 1.5rem;    /* 24px */
    --space-xl: 2rem;      /* 32px */
    --space-2xl: 3rem;     /* 48px */
    --space-3xl: 4rem;     /* 64px */
}
```

## Typography Tokens (major third scale: 1.25)

```css
:root {
    --text-xs: clamp(0.7rem, 0.66rem + 0.2vw, 0.8rem);
    --text-sm: clamp(0.8rem, 0.76rem + 0.2vw, 0.9rem);
    --text-base: clamp(0.95rem, 0.91rem + 0.2vw, 1.05rem);
    --text-lg: clamp(1.125rem, 1.05rem + 0.35vw, 1.3rem);
    --text-xl: clamp(1.4rem, 1.3rem + 0.5vw, 1.65rem);
    --text-2xl: clamp(1.75rem, 1.6rem + 0.75vw, 2.1rem);
    --text-3xl: clamp(2.2rem, 2rem + 1vw, 2.8rem);
}
```

## Radius and Transition Tokens

```css
:root {
    --radius-sm: 0.375rem;
    --radius-md: 0.625rem;
    --radius-lg: 1rem;
    --radius-full: 9999px;

    --ease-out: cubic-bezier(0.16, 1, 0.3, 1);
    --duration-fast: 150ms;
    --duration-normal: 250ms;
}
```

## Typography Rules

1. Maximum two font weights: 400 (regular) and 600 (semibold)
2. Line height: 1.6 for body, 1.2 for headings
3. Max width: 65ch for body text
4. One `<h1>` per page, never skip heading levels
5. Use `clamp()` tokens for fluid scaling

```css
body { font-size: var(--text-base); line-height: 1.6; color: var(--text-1); }
h1, h2, h3 { line-height: 1.2; letter-spacing: -0.02em; }
h1 { font-size: var(--text-3xl); }
h2 { font-size: var(--text-2xl); }
h3 { font-size: var(--text-xl); }
```
