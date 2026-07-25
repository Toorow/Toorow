# Campaign Manager 360 rollout notes

The module pins `dfareporting-v5`, contains 14 curated fields and materializes
compatibility for STANDARD, FLOODLIGHT and REACH. Every field is exposed and the
generated catalog has zero planned and zero drift fields. PATH_TO_CONVERSION is
unavailable until its dedicated row grain exists.

Local tests cover profile/advertiser discovery, access checks, pre-call compatibility,
bounded `nextPageToken` pagination, the 60-second request boundary, async report state
mapping/request hashing, full-grain landing and non-additive reach/frequency semantics.

Public verification remains blocked until an authorized profile proves discovery,
bounded and saved report execution, compatibleFields parity, provider sentinels,
pagination, quota behavior and redacted Google error evidence.
