# Ember tokens

Canonical hex. Desktop: `src/renderer/styles.css` `:root`. If a landing page is recreated later, alias the same names with a `--bg-*` prefix there — don't introduce a third set of hex.

## Color

| Role | Hex | Desktop | Landing alias |
| --- | --- | --- | --- |
| Page | `#f6f4f0` | `--page` | `--bg-page` |
| Recessed | `#efece7` | `--canvas` `--field` | — |
| Surface | `#fffcf8` | `--surface` | `--bg-card` |
| Inset | `#f3f0eb` | `--inset` | `--bg-inset` |
| Hover | `#ece8e2` / `#e3ded6` | `--hover` `--hover-2` | `--bg-hover` |
| Ink | `#1c1917` | `--ink` `--accent` | `--ink-primary` |
| Ink hover | `#0c0a09` | `--accent-ink` | — |
| Secondary | `#5c574f` | `--ink-2` | `--ink-secondary` |
| Tertiary | `#9a948b` | `--ink-3` | `--ink-tertiary` |
| Line | `#e8e4dc` / `#ddd8ce` | `--line` `--line-strong` | `--line-subtle` `--line-strong` |
| Tint | `#efeae3` | `--accent-tint` | — |
| Green | `#2f7d4a` / `#e7f3eb` | `--green` `--green-tint` | same names |
| Red | `#c4473a` / `#f8ecea` | `--red` `--red-tint` | same names |
| Leather (brand accents only) | `#8a6f5a` / `#f4ece4` | do not use on desktop chrome | `--brand` `--brand-tint` |

Do not add purple/blue brand colors. Syntax highlighting uses ink + green, not a third hue.

## Shadow

| Token | Use |
| --- | --- |
| `--shadow-hairline` | Bubble, input, default card |
| `--shadow-btn` | Compact controls |
| `--shadow-card` | Raised cards (approval) |
| `--shadow-raised` | Menus |
| `--shadow-overlay` | Modal panel, toast |

Prefer hairline on new cards.

## Type

- Sans: `"Inter Variable", "PingFang SC", "Microsoft YaHei", system-ui, sans-serif`
- Mono: `ui-monospace, "SF Mono", Menlo, "Cascadia Mono", Consolas, monospace`
- Workbench body: `14px / 1.5`, tracking `-0.011em`
- Markdown: h1 `16px`, h2 `15px`, h3 `14px`

## Desktop classes

| Class | Role |
| --- | --- |
| `.app` | Flex shell |
| `.sidebar` | 252px rail |
| `.prompt-wrap` / `.prompt-shell` | Floating composer |
| `.prompt-wrap.hero` | Empty-state composer |
| `.prompt-tag` / `.prompt-tag.link` | File / URL chips |
| `.user` / `.user-link` | User bubble + URL chip |
| `.turn` / `.markdown` / `.stream` | Assistant reply |
| `.trace` | Thinking + tools |
| `.approval` / `.approval-cmd` | Permission card; command in `<details>` |
| `.combo` / `.permission-combo` | Mode picker |
| `.ghost` / `.primary` / `.send` | Text / filled / round send |
| `.modal` / `.panel` | Overlay + dialog |

## Motion

`prefers-reduced-motion`: stop caret/trace/logo dash. No page-load choreography.
