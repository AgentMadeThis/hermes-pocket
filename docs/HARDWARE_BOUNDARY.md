# Hardware and source boundary

The demonstrated compact handheld is an ESP32-S3-class portrait 240x320 touch
radio device, with a 240x268 application area inside native device chrome. The
host remains the P2 player backend. The compact design adds a scrollable cached
library, explicit load-more, tap selection and observed volume controls without
background catalog polling or automatic mutation retry.

This package distributes the compatible Python host protocol/backend only.
It intentionally excludes native UI/firmware, firmware binaries, toolchains,
vendor SDKs, fonts, private binding transforms and device installation helpers.
The firmware target includes separate third-party/GPL provenance obligations;
complete corresponding-source/build closure was not qualified for this public
package. MIT here does not apply to that firmware. Do not call this a fully
reproducible firmware release or use this package as a device flashing guide.

Local installation records distinguish a compact firmware installation from
earlier player radio tests. Recorded earlier protected P2 exchanges and fresh
controller observations are not compact touchscreen or audible-output proof.
The latest inspected compact record still lists owner-driven entry, paging,
selection, volume, exit/re-entry and hang resolution as pending. An owner's
subsequent acceptance is separate evidence, not fabricated by the host tests.
No new hardware operation was performed to prepare this source package.
