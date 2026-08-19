# P2 Bridge BACnet Scanner

A lightweight read-only BACnet/IP scanner with a Tkinter GUI, built on
[bacpypes3](https://github.com/JoelBender/BACpypes3) — sized for verifying a
[P2_BACnet_Bridge](https://github.com/jamesccupps/P2_BACnet_Bridge) instance.

Single file, no install step beyond `pip`, no config files.

## What it does

- Sends Who-Is and lists I-Am responses (instance, source, vendor)
- For a selected device, reads identifying properties (object-name,
  vendor, model, firmware, segmentation, system status)
- Reads `object-list` (whole-array fast path, falls back to indexed walk
  when the response overflows the negotiated APDU — the canonical case
  is Trane Tracer SC+ with 4000+ objects)
- Bulk-reads `object-name`, `description`, `present-value`, `status-flags`,
  `reliability`, and `units` for every object via ReadPropertyMultiple,
  starting **automatically** as soon as the object list finishes loading
- Decodes `status-flags` to readable labels (`ALARM`/`FAULT`/`OVR`/`OOS`)
- Highlights faulted points in red so #COM and `COMMUNICATION_FAILURE` are
  obvious
- **Property tree** (right pane): click any object on the left to see every
  property it exposes, fetched on demand via property-list discovery + RPM —
  YABE-style without the YABE
- Filter box for searching by name/description (e.g. `NODE1.AHU1`)
- CSV export of the full scan

It will **never** issue a write — there is no write path in the code.

## Quick start

```
pip install -r requirements.txt
python p2_bridge_scanner.py
```

1. **Start stack** — bind address auto-fills with your detected IP plus
   `/24:47809`. The non-default port (47809) avoids clashing with the bridge
   running on 47808 on the same host. Local instance defaults to a random
   value in the test range. Click *Start stack*.
2. **Who-Is** — leave Target blank for local broadcast, or enter the bridge
   host (e.g. `192.168.1.50`) to unicast directly. Click *Who-Is*.
3. Click the bridge in the discovered devices list — identifying properties
   load into the upper-right pane.
4. Click **Load object list** — RPM auto-fires once the list is in, so the
   Value/Units/Flags/Reliability columns populate without a second click.
   The button stays available for manual refreshes.
5. Click any object row to see its full property set in the right pane.
   *Refresh* re-reads just that object.

## Robustness against large devices

Two cases the scanner handles automatically:

- **Buffer overflow on object-list reads.** Trane Tracer SC+ (and any other
  device whose object list exceeds the APDU MTU) will reject a
  whole-array read with `buffer-overflow`. The scanner falls back to reading
  the array length first and walking individual indices, which always fits.
- **Buffer overflow on RPM chunks.** If a 15-object RPM exceeds the APDU,
  the chunk gets halved and retried — recursively down to chunks of 1.
  Only after `chunk_size=1` still fails does it fall back to per-property
  reads.

Both fallbacks are silent in the UI; the log line afterward shows total
elapsed time and any per-object errors.

## What to verify against the P2 bridge

| Bridge claim | How to verify here |
|---|---|
| Device announces with configured instance | Who-Is → I-Am should return your `bacnet_device_instance` (default 599001) |
| Object count matches manifest | Load object list → count should match `manifest.json` size |
| AI/AV/BI/BV split is correct | Log line after object-list shows breakdown by type |
| Engineering units mapped from APOGEE | Units column populated for analog points |
| Description carries device + slot + units | Description column shows `slot N, app NNNN, UNIT` |
| #COM faults propagate (`reliability=NO_OUTPUT`) | Reliability column shows `noOutput`, row turns pink |
| Lost-PXC points show `COMMUNICATION_FAILURE` | Reliability column shows `communicationFailure` |
| Names follow `NODE.DEVICE.POINT` convention | Filter on `NODE1.` to confirm |
| RPM segmentation works at scale | Time the full read; ~786 points should complete in seconds |

## Same-host setup (bridge + scanner on one machine)

If you're running the scanner on the same machine as the bridge, you need a
different UDP port — the default `47809` in the bind address handles that.
Then either:

- Target the bridge's IP directly in Who-Is (`192.168.1.50`), or
- Send to local broadcast (leave Target blank). The bridge will respond from
  port 47808 to your port 47809.

## Cross-subnet

If the bridge is on a different subnet you can either bind directly to the
bridge's subnet (multi-homed host) or — if the bridge ever grows BBMD
support — register as a foreign device. The current bridge v0.1 doesn't
expose BBMD, so a routable host or a multi-homed scanner is the path.

## Layout

```
p2_bridge_scanner.py       — single-file GUI + bacpypes3 wrapper
tests/test_status_flags.py — status-flags decoding and its edge cases
requirements.txt           — bacpypes3
requirements-dev.txt       — adds pytest
README.md                  — this file
```

## Development

```
pip install -r requirements-dev.txt
pytest
```

The tests cover the status-flags decoder, which is the piece the rest of the
UI leans on: rows are highlighted from the decoded string, and the fault count
in the summary line is derived from it. Decoding those four bits by attribute
name does not work — bacpypes3 exposes `StatusFlags.fault` and `.overridden`
as bit-position constants rather than per-instance values, so reading them
that way reports a fault on every point. They are read positionally.

## Notes

- The scanner runs the asyncio event loop in a background thread so the Tk
  UI stays responsive during long reads.
- Object-list fast path (`read_property` on the array as a whole) requires
  the bridge to support segmentation — bacpypes3 handles this automatically
  when it does. If segmentation isn't supported the scanner falls back to
  indexed reads automatically.
- RPM chunk size is 15 by default. Bridges with very long object names may
  need a smaller chunk; edit `chunk_size` in `read_object_summary` if you
  hit APDU size errors.

## License

MIT — see [LICENSE](LICENSE).
