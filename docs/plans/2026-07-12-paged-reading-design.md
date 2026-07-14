# Paged reading with 3D page-turn (v0.2d)

## Goal
Replace scroll reading with book-like pages: swipe or bottom arrows to turn,
3D page-flip animation, position remembered per page turn.

## Decisions (with David, 2026-07-12)
- 3D page-turn animation (perspective flip around the page edge, fold shadow).
- Paged mode only — scrolling mode is removed, less UI and code.

## Pagination
- `#text` becomes a fixed-height viewport (`overflow: hidden`); inside it a
  `#pages` container lays the chapter out in CSS columns of exactly the text
  area's width. The browser does the page breaking.
- Page n = `translateX(-n * (pageWidth + gap))` on `#pages`.
- `pageCount = round(scrollWidth / (pageWidth + gap))`.
- Word spans, marks, popovers keep working — the DOM is unchanged, only the
  layout differs.

## Position
- Server schema unchanged: `positions(book_id, chapter_idx, para_idx)`.
- On page turn: `para_idx` := first paragraph whose column offset lands on the
  current page → `savePosition()` (debounced).
- On open/restore/resize: recompute the page containing `para_idx` and jump
  there without animation. Viewport-size independent by construction.
- `jumpToSource` flashes the paragraph and goes to its page instead of
  scrolling.

## Page-turn animation
- Final (after trying a full 3D flip, David: "weniger Animation bitte"):
  a quiet 200 ms `translateX` slide of the column container (`slideTo`),
  ease-out. Reduced-motion users get an instant switch.
- The 3D page-flip overlay was built, debugged and then removed again. If it
  ever comes back, three hard-won constraints (found by freeze-frame
  debugging) must hold:
  1. Perspective lives INSIDE the sheet's own transform
     (`perspective(…) rotateY(…)`), never on the shared parent — a parent
     `perspective` puts sheet and page into one 3D rendering context that
     paints by DEPTH, so the flat page bleeds through the rotating sheet.
  2. The sheet rotates AWAY from the viewer (positive rotateY, 0→90°): the
     projection shrinks toward the spine and stays inside the page box — no
     clipping, no magnification, no backface pop past 90°.
  3. Dark theme needs a brightness cue, not shadows (black-on-black is
     invisible): a `flip-veil` dims the revealed page and fades with the
     turn, so the new page "steps out of the sheet's shadow".

## Navigation
- Swipe: touch/pointer horizontal drag past threshold (~50 px) turns the page.
- Footer bar: ‹ arrow, "page 3 / 12" (chapter-local), › arrow.
- Keyboard: ← / →. Chapter boundaries: turning past the last page loads the
  next chapter at page 0; before the first page loads the previous chapter at
  its last page.

## Testing
- Pagination math depends on real browser layout (CSS columns), so no unit
  harness (project is stdlib/vanilla by design). Verified end-to-end in the
  embedded browser: page count, turn animation, boundary turns, position
  restore after reload, resize reflow.
