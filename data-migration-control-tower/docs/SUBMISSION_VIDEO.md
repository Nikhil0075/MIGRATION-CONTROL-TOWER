# Submission video — shot list and edit sheet

Six 5-second clips, cut in order, giving a 30-second silent loop. Generated
with Wan 2.7 at 720p from six purpose-made still frames (GPT Image 2, 2K),
so the palette and level of abstraction hold across the cuts.

**Captions are added in the editor, not by the model.** Image and video
models render lettering unreliably — the empty-state sheet came back with
captions under every cell despite being told not to, and garbled text is
the fastest way to make a submission look unfinished. Every prompt here
carries `text, letters, numbers, captions` in its negative prompt, and the
caption column below is for the edit, not for generation.

Clips are in `docs/video/`.

## Cut order

| # | Clip | Beat | Caption to add in the editor |
|---|---|---|---|
| 1 | `shot1-legacy-estate.mp4` | A legacy estate sits dormant: three source databases, nothing flowing. | "A legacy estate. Nothing has moved yet." |
| 2 | `shot2-discovery.mp4` | Discovery sweeps the estate and a catalogue assembles. | "Discovery catalogues it — tables, columns, pipelines." |
| 3 | `shot3-fleet-activates.mp4` | Seven agents register and light up around the control tower. | "Seven specialists activate, resolved by capability." |
| 4 | `shot4-row-loss.mp4` | One pipeline breaks; rows fall away; that line alone turns amber. | "Validation catches a row loss. One line, not the run." |
| 5 | `shot5-memory-recall.mp4` | The memory vault supplies a prior confirmed fix; checks turn green. | "Memory recalls a confirmed fix. Reconciliation still decides." |
| 6 | `shot6-cutover-complete.mp4` | The gate opens, the flow completes into the warehouse, audit lines settle. | "A human approves. Cutover completes, fully audited." |

## Notes for the edit

- **Mute every clip on import.** The brief called for a silent loop, and
  these are NOT silent: Wan 2.7 attaches an AAC track, confirmed by finding
  a `soun` handler and an `mp4a` sample entry in each file. It is model-
  generated ambience nobody chose, so it must be muted rather than
  inherited. If music is added, keep it under the captions rather than
  cutting to it.
- **Restrained motion is deliberate.** Every prompt specifies a locked-off
  camera and slow pacing. This is an operations story; a showreel edit with
  fast whips would undercut the "trustworthy and inspectable" claim the
  product makes.
- **Colour carries meaning and should not be graded away.** Blue is normal
  flow, teal is confirmed or recalled knowledge, amber is risk. Shot 4 is
  the only amber in the piece; that is the point of it.
- **Loop point.** Shot 6 ends calm and shot 1 opens calm, so a straight cut
  from 6 back to 1 loops without a jolt.
- If a clip needs to be reshot, the still frame it was animated from is the
  cheap thing to regenerate first — the frame decides the composition, and
  animating a bad frame costs the full clip price.

## Honest limits

- These are illustrations of the architecture, not a screen recording. If
  the submission needs proof of the software running, use the branded
  evidence frames instead, which are captured from the real console and
  labelled `Live` / `Local` / `Simulated`.
- The seven nodes in shot 3 are drawn, not counted from the registry. They
  match the seven agents that exist, but the video is a narrative asset and
  should not be presented as telemetry.
