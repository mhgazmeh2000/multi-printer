# Cartridge Change Event Contract

## Event identity

Every confirmed cartridge transition is stored as one event:

- `type`: `CARTRIDGE_CHANGED`
- `details.confidence`: `CONFIRMED`, `PROBABLE`, `POSSIBLE`, or `UNKNOWN`
- `details.detection_method`: the primary method for backward-compatible consumers
- `details.detection_methods`: all non-unknown methods contributing to the decision
- `details.evidence`: the complete structured evidence list
- `details.old_identity` and `details.new_identity`: normalized identities when available
- `details.event_id`: deterministic event identity used for duplicate protection

The primary method is selected by evidence strength. A real chip identity change is
`chip_id`; a decision based on multiple generic signals is represented by its strongest
method while all contributing signals remain in `detection_methods` and `evidence`.

## Compatibility

The event type remains `CARTRIDGE_CHANGED`. Existing consumers can continue reading
`detection_method`, `prev_cartridge_id`, and `cartridge_id` when the primary evidence is
an actual chip identity change. These fields are aliases of the generic event payload;
they are not synthetic replacements for missing evidence.

Database readers expose the decoded payload both as `details` and as top-level fields.
The JSON payload is lossless, so evidence survives `add_event()` -> SQLite -> `get_log()`.

## Decision rules

- A first observation establishes baseline state and emits no event.
- Same identity on later polls emits no event.
- A genuine chip or serial change can emit immediately, subject to signal quality and
  reboot/missing-poll gates.
- A level jump or counter reset alone does not confirm a replacement.
- Correlated counter, remaining-level, and supply-life changes can produce `PROBABLE`.
- Reboot artifacts, missing polls, static identity values, and manual override suppress
  unsafe decisions.
- One physical transition produces one event even when several signals change together.

## State model

`PrevStore` keeps a per-printer snapshot. The snapshot contains a `cartridge_state` map
keyed by cartridge color/slot. Each slot contains the latest identity, counters, levels,
quality metadata, confidence, and last event information. Partial polls merge into the
previous slot without erasing values that were not observed. The snapshot is persisted
through the printer-counter database row so a process restart does not create a false
baseline.
