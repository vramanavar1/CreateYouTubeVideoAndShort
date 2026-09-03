# Background audio — licences

Drop the tracks you want used as background music into this folder
(`.mp3`, `.m4a`, `.wav`, `.aac`, `.ogg`, `.flac`). The pipeline picks one
deterministically per job, so the same job always renders with the same track.

**The audio files themselves are gitignored.** Only this manifest is tracked —
music is not ours to redistribute.

## Why not the track from the PRD?

The PRD asked to use the audio from a specific YouTube music video. Downloading
audio from YouTube breaks its [Terms of Service](https://www.youtube.com/t/terms),
and that track is copyrighted, so the pipeline does not fetch it. Publishing a
Short with it would also collect a Content ID claim within minutes.

## Where to get tracks you can actually use

| Source | Notes |
|---|---|
| **[Mixkit](https://mixkit.co/free-stock-music/)** | **No account, no attribution.** What this project uses. Commercial use permitted on web and social platforms. See the licence recorded below. |
| **YouTube Audio Library** (YouTube Studio → Audio library) | Needs a Google sign-in, but it is the only source where Google guarantees tracks "won't be claimed by a rights holder through the Content ID system". Filter to "No attribution required", or record the required credit below. |
| Purchased / subscription libraries (Epidemic Sound, Artlist, …) | Check the licence covers monetised uploads if that matters to you. |
| Tracks you made or own outright | Nothing to record beyond a note. |

Avoid anything whose free tier is conditioned on a plan you are not on — that is
how the previous track had to be thrown away. See `docs/decisions.md`.

## Record what you used

| File | Source | Licence | Attribution required? |
|---|---|---|---|
| `SereneView-ByArulo.mp3` | "Serene View" by Arulo, from [Mixkit](https://mixkit.co/free-stock-music/). Downloaded 2026-09-03, no account required. Original filename `mixkit-serene-view-443.mp3`; sha256 `556ae7a19c783bd6…` | Mixkit Stock Music Free License (quoted below) | **No** |

Keep this table current. When a Short gets a copyright claim, this is the
document that tells you whether the claim is wrong.

Where a track does require a credit, put the exact wording in
`YTSHORT_AUDIO_CREDIT` (see `.env.example`) as well as in the table. The pipeline
appends it to every YouTube description automatically, because the obligation
applies to every upload and a human will eventually forget one.

`ytshort doctor` warns about any track in this folder with no row above. That
warning is the guardrail — it is what stops a file being published on a licence
nobody checked.

## Mixkit Stock Music Free License — the terms, verbatim

Recorded here because Mixkit's licence page is JavaScript-rendered and cannot be
fetched as plain text later. If a claim is ever disputed, this is the wording that
was in force when the track was downloaded (2026-09-03):

> Items under the **Mixkit Stock Music Free License** can be used in your
> commercial and non-commercial projects for free.
>
> You're permitted to download, copy, modify, distribute and publicly perform the
> Music Items on any web or social media platform, including internet-based video
> on demand services, podcasts and advertisements.
>
> You're not allowed to use them in CDs or DVDs, video games or tv or radio
> broadcast. You're also not allowed to remix them (or incorporate in a music-only
> track), claim them as your own or register them on any rights management service.

And from their music page: *"All audio clips are royalty free and can be used with
no attribution or sign up required."*

**Why none of the prohibitions bite here.** This pipeline publishes a video to
YouTube — a web platform, explicitly permitted. It lays the track under video
rather than remixing it into a music-only work. It claims no ownership and
registers nothing with a rights management service.

### The residual risk, stated rather than buried

Mixkit offers **no equivalent of Google's Content ID guarantee**. The YouTube Audio
Library is the only source where Google states tracks "won't be claimed by a rights
holder through the Content ID system"; Mixkit instead forbids *others* from
registering its tracks with a rights management service, which lowers the odds of a
claim without removing them.

That was a deliberate trade: Mixkit needs no account, and the Audio Library does.
If a Content ID claim would cost more than a sign-in, switch to the Audio Library —
nothing in the pipeline changes, only this row. Full reasoning in
`docs/decisions.md`.

Uploads stay `private` by default, and YouTube force-locks them to private until
the compliance audit clears, so exposure today is low either way.
