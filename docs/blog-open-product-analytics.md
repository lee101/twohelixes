# Product analytics should end in a question, not a report queue

Published on twoHelixes on 30 August 2026.

We connected Netwrck and eBank to the same first-party data platform that draws
their charts, then made the useful surface portable as an open Go CLI.

The native tracker, GA4 Measurement Protocol, Segment Tracking API, Mixpanel
event/profile APIs, and Amplitude HTTP V2 now land in one owned event model.
Sampling is off for ordinary traffic and becomes adaptive only above a
configured event rate. A probability applied upstream is recorded but never
drawn twice; it composes with the server probability into one inverse weight.
Important identity, group, purchase, sign-up, refund, login, and error events
are never server-sampled.

An owned analytics site can be passed directly to the normal `/v1/query` chart
agent. The standalone `twohelixes-cli` triggers those graph runs, renders the
Plotly response in a terminal, reads summaries and recent events, builds
ordered funnels, exports Segment-shaped batches, and asks chart questions of
the product event stream. Its schema command discovers arbitrary custom event
names and property types, and its track command can emit native, Segment, GA4,
Mixpanel, or Amplitude payloads during a migration. Path discovery shows the
journeys people actually took, while revenue stays separated by currency so a
dashboard never adds NZD and USD into a plausible-looking fiction.

The published route is `/blog/open-product-analytics`. The canonical article
body lives in `routes/pages.py`; this Markdown copy is kept for editing and
syndication.
