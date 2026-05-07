---
name: visual-crawl
description: "Autonomous visual crawl of a deployed web app. Screenshots at multiple viewports, compares against design tokens, files GitHub issues for findings."
argument-hint: "[--url <base-url>] [--auto] [--output github|session]"
---

# Visual Crawl

Autonomous frontend quality assurance.

## Flow

1. Discover all routes (crawl links, check sitemap, read router config)
2. For each route, screenshot at 3 viewports:
   - Mobile (375px)
   - Tablet (768px)
   - Desktop (1440px)
3. Check against design system tokens (if /design-norms skill loaded)
4. Test basic interactions (navigation, forms, buttons)
5. File GitHub issues for every finding with screenshots

## Checks

- Visual consistency across viewports
- Responsive breakpoint behavior
- Color/typography against design tokens
- Interactive element states (hover, focus, disabled)
- Accessibility basics (contrast, alt text, focus order)
- Layout shifts and overflow

## Output

### --output github (default)
Creates one GitHub issue per finding with:
- Screenshot evidence
- Viewport and route
- Expected vs actual
- Severity label

### --output session
Reports findings in chat without creating issues.

## Rules

- Load design norms before crawling (if available)
- Don't file issues for known/intentional deviations
- Group related findings into single issues
