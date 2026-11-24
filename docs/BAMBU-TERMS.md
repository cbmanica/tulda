# Bambu Studio: licensing and terms review

Good-faith engineering review, fetched 2026-11-24. Not legal advice. Inferences are marked
"(inference)". tulda is independent and is not affiliated with or endorsed by Bambu Lab.

## What tulda does with Bambu software

- Runs the user's own locally installed Bambu Studio CLI (`--export-png`, plus
  `--camera-view N`) as a separate subprocess (`tulda/adapters/builtin.toml`,
  adapter `bambu-export-png`). Values 4-7 for `--camera-view` are not in `--help`; they were
  found by probing Studio 02.09.01.70 and may change or vanish.
- Saves the PNG and serves it read-only over HTTP on the user's LAN/tailnet.
- Does not bundle, copy, link or redistribute any Bambu code. It does not import Bambu
  libraries.
- Verified in code (grep of `tulda/` for cloud, plugin, mqtt, token, password, login): no
  call to Bambu cloud, MakerWorld, the networking plugin, authentication, or any printer.
  The only Bambu touchpoint is the CLI subprocess. The optional `bambu-plate` adapter
  (unzipping an embedded `Metadata/plate_N.png`) is a TODO and not implemented.
- Planned: read the user's own `.3mf` with `zipfile`/XML and convert to GLB for a browser
  viewer. This is the same file-format reading as below.

## Findings

### 1. Bambu Studio's licence

- Source: https://github.com/bambulab/BambuStudio (README, fetched 2026-11-24):
  "Bambu Studio is licensed under the GNU Affero General Public License, version 3."
  The LICENSE file (https://github.com/bambulab/BambuStudio/blob/master/LICENSE, fetched
  2026-11-24) is the standard AGPL-3.0 text with no added terms in its header. I did not
  read the whole file or every source-file header, so added AGPL section 7 terms elsewhere
  are not ruled out.
- README, same fetch: "The bambu networking plugin is based on non-free libraries", and it
  is described as optional ("provides extended networking functionalities"). The README
  does not mention the CLI or trademarks.
- Software Freedom Conservancy (https://sfconservancy.org/news/2026/may/18/bambu-studio-3d-printer-agpl-violation-response/,
  fetched 2026-11-24) says Bambu Studio is a modified PrusaSlicer (AGPLv3) and that the
  proprietary networking libraries violate the AGPL. That is SFC's position in a dispute,
  not a finding.
- Implications (inference): the AGPL obligations (source offer, same licence) fall on whoever
  conveys or modifies Bambu Studio. Running it as a separate process and reading its output
  files does not make tulda a derivative or "covered work"; tulda neither links nor bundles
  it. Apache-2.0 for tulda is therefore not in conflict. AGPL section 13 concerns users who
  interact remotely with a modified Studio; tulda exposes only PNGs from an unmodified
  install, not Studio itself (inference).

### 2. Bambu Lab Terms of Use / EULA / cloud terms

NOT VERIFIED. I could not read Bambu's own terms:

- https://bambulab.com/en-us/policies/terms and https://bambulab.com/en-us/legal returned
  HTTP 403; https://us.store.bambulab.com/agree-terms returned 402; the Wayback Machine is
  blocked from this tool. I did not see the software licence text shown at install time.
- Secondhand only (unverified forum repost,
  https://groups.google.com/g/3d-printing-tips--tricks/c/PzNhy_lBytk): a non-exclusive
  licence "on one Bambu Lab device", no commercial use, no modifying or reverse engineering
  the software. Treat as unconfirmed.
- Bambu's own blog (https://blog.bambulab.com/setting-the-record-straight-on-cloud-access-and-community/,
  fetched 2026-11-24): "Our cloud is a private service. Access to it is governed by a user
  agreement, not the AGPL license." and "impersonating official clients in communication
  with our cloud infrastructure is not allowed." This concerns the cloud and the plugin,
  which tulda does not touch. Bambu's position on terms overriding AGPL is disputed by SFC.
- Not found anywhere I could read: any clause on CLI automation, undocumented flags,
  sharing rendered previews, or third-party tool naming/trademark. Absence of evidence
  only. Bambu Lab is a trademark; use it descriptively only.

### 3. File formats

- 3MF: https://www.3mf.io/ (fetched 2026-11-24): "An open, XML-based standard", ISO/IEC
  25422:2025, specification licence listed as "Free & Open Source", no licensing fees;
  the page states no usage restrictions.
- Bambu extensions (`Metadata/*.config`, `project_settings`) are ordinary entries in the
  3MF zip. I found no published licence or restriction on them (could not check Bambu's
  terms; see section 2). Parsing a user's own file with stdlib is ordinary interoperability
  (inference). 3MF has an optional "Secure Content" encryption feature; tulda must not try
  to bypass any encrypted/protected content.

### 4. Cloud, plugin, authentication, printers

None touched, by code inspection (see above). Keep it that way: any future feature that
loads the networking plugin, logs in, or talks to a printer or MakerWorld would bring Bambu's
cloud user agreement into scope, which I could not read.

## Conclusions

| Bucket | Verdict | Basis |
|---|---|---|
| (a) One user, own machine, own models | Compliant (high confidence on AGPL; terms unread) | Separate process, no bundling; AGPL permits use. Terms unverified, but private personal use of a licensed install is the ordinary case. |
| (b) Public repo, users run it against their own Studio install | Compliant, with unclear residual risk | tulda ships no Bambu code (AGPL distribution duties don't arise). Unclear: whether Bambu's terms object to automating the CLI or using undocumented `--camera-view` values (unread). Trademark: descriptive use only, with a non-affiliation notice. |
| (c) Hosted/shared service, others access it or submit their models | Unclear, leaning likely a problem | Running one licensed install to serve other people may exceed a per-user/per-device licence (the unverified "one device" wording), and publicly displaying renders of others' models raises content/IP questions; AGPL section 13 would matter if Studio were modified. Rests on terms I could not read. |

## Recommendations

1. Use tulda only with your own models and your own licensed Studio install; do not expose
   it publicly or run it as a service for third parties, until the Terms of Use have been
   read in full (or Bambu is asked).
2. Read the current Terms of Use at https://bambulab.com/en-us/legal and the licence shown at
   Studio install. Re-check any clause on automation, reverse engineering and commercial use.
3. Keep tulda away from the networking plugin, cloud, authentication and printers.
4. Treat `--camera-view` 4-7 as unstable; there is a documented fallback plan (OpenSCAD render
   of an extracted mesh) in `docs/PLAN.md`.
5. For the GLB phase, convert only the user's own files, do not bypass 3MF encryption, and do
   not republish converted meshes of third-party models.
6. Keep the non-affiliation notice and use "Bambu Studio" only to name the tool being run.
