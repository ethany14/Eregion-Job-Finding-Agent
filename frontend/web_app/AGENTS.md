# Eregion frontend conventions

Preserve the established Eregion interface: the v4-final layout, the v3-inspired
palette, and a Day/Night toggle. Do not add background waves or a compass motif.
Extend the implemented design when adding or repairing features.

## Source of truth

- `tokens.css` defines palettes, typography, radii, and shadows. Keep
  `../chrome_extension/shared/tokens.css` aligned; that copy omits the font import.
- `eregion.css` supplies the Eregion visual layer and loads after the base layout
  in `apply.css`.
- `apply.html` owns the shell, SVG sprite, and pre-paint theme initialization.
- `theme.js` stores the user's `eregion.theme` choice as `light` or `night`.
  Without a saved choice, the app follows the system setting; the base palette
  is Night.
- `chat-typography.css` owns message typography and Markdown presentation.
- `i18n.js` owns the English / Simplified Chinese interface (`eregion.lang`, the 中/EN switch
  beside Day/Night). Write interface text in English inside `t()` / `tn()` and add its
  translation to `i18n-zh.js`; `tests/test_i18n.mjs` fails on a missing one. Never translate
  chat messages, employer form labels or option values, or the person's own data.
- `career-chat.js`, `apply-answers.js`, `pages.js`, `insights.js`, and
  `conversations.js` own cards, questions, views, and workspace interactions.
- When available locally, `../../design/v4-final/` contains reference mockups and
  `../../design/archive/` contains earlier versions. These ignored assets are
  optional for a checkout; implemented files take precedence.

## Visual language

- Night uses a holly-dark surface and silver ink; Day uses parchment. Use
  semantic variables: `--holly` for progress/success, `--signal` only for
  "needs you", `--gold` for offers or sealed documents, `--river` for information,
  and `--danger` for destructive actions.
- Use `--map` (IM Fell English SC) for place names, page titles, and labels;
  `--serif` (EB Garamond) for headings, greetings, and large numbers; `--sans`
  (Inter) for UI text; and `--mono` for technical snippets.
- Panels are sheets with a thin frame, two corner ticks, 3–4px radii, and generous
  whitespace. Do not put decorative patterns, waves, or textures behind content.
- Show processes with `ol.trail` and `li.st` states `done`, `now`, `wait`, or
  `gold`. Use `.trail.compact` in rows. Retain application stages
  Sighted → Prepared → Sent → In interview → Offer and material stages
  Drafted → Tempered → Sealed.
- Give each card one dominant pill-button action. Keep secondary actions quiet
  and reuse the SVG sprite's icons and stroke style.

## Interaction and layout

The sidebar contains Chronicle, Council, The Road, The Forge, Job alerts,
"Beacons lit" conversations, Archive with Archived/Trash tabs, and the profile
and theme controls. These are views within the `/apply` shell.

Chronicle is the empty career state, with the greeting, composer, today's entries,
beacons, and the road. Keep the conversation central and The Lookout as the
optional right panel. Preserve stepped questions, batch answer submission,
unsent drafts, existing IDs, test hooks, and API wiring.

Maintain mobile drawers, keyboard and focus behavior, and reduced-motion support.
For visible changes, inspect the affected state in Day and Night at desktop and
mobile sizes. A requested new design version means a new layout and structure;
preserve the existing version in the local `../../design/archive/` before replacing
it, rather than treating a color change as a new version.
