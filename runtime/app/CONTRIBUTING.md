# Contributing to GenericAgent

## Why This File Is Short

GenericAgent's core is ~3K lines. Every file in this repo will be read by AI agents — potentially thousands of times. Extra words cost real tokens and push useful context out of the window, increasing hallucinations. This document practices what it preaches: **say only what matters.**

## Before You Contribute

1. **Read the codebase first.** It's small enough to read in one sitting. Understand the philosophy before proposing changes.
2. **Open an Issue first** for anything non-trivial. Discuss before coding.

## Code Standards

All PRs go through a strict automated code review skill. Key expectations:

- **Self-documenting code, minimal comments.** If code needs a paragraph to explain, rewrite it.
- **Compact and visually uniform.** Fewer lines, consistent line lengths, no fluff.
- **Small change radius.** Changing A shouldn't ripple through B, C, D.
- **More features → less code.** Good abstractions make the codebase shrink, not grow.
- **Let it crash by failure radius.** Critical errors fail loud; trivial ones pass silently. No blanket try-catch.

> ⚠️ This review is deliberately strict — most AI-generated code (e.g. Claude Code output) will not pass as-is. Read the full principles before submitting.

## Skill Contributions

GenericAgent evolves through skills. Not all skills belong in the core repo:

| Type | Where it goes | Example |
|---|---|---|
| **Fundamental / universal** | Core repo (`memory/`) | File search, clipboard, basic web ops |
| **Domain-specific / niche** | Skill Marketplace *(coming soon)* | Stock screening, food delivery, specific API integrations |

If your skill only makes sense for a specific workflow, it's a marketplace candidate, not a core PR.

## PR Checklist

- [ ] Issue linked or context explained in ≤3 sentences
- [ ] Code passes the [review principles] self-check:
  1. Can I safely modify this locally without reading the whole codebase?
  2. Is there a clear core abstraction — new features add implementations, not modify old logic?
  3. Are change points converging at boundaries, not scattered everywhere?
  4. On failure, can I quickly locate the responsible module?
- [ ] Net line count: ideally negative or zero for refactors
- [ ] No unnecessary dependencies added
