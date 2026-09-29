# AstrBot pipeline test snapshot

The excerpts in this directory are the installed AstrBot 4.26.6 methods used to
verify plugin-handler dispatch and image command propagation. They are reduced
to the exact methods under test and are not imported as an AstrBot runtime.
The accompanying tests run with fake message and model objects; they never
start a bot or contact an image service.

The installed runtime identifies its AstrBot license at
`app/astrbot-4.26.6.dist-info/licenses/LICENSE`; review third-party terms before
redistributing these excerpts beyond this repository's internal CI fixtures.
