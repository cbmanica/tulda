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

Bambu's own Terms of Use were read in full in the 2026-11-24 review; see the next section.
The earlier attempt to fetch them failed (HTTP 403 from https://bambulab.com/en-us/policies/terms
and https://bambulab.com/en-us/legal; HTTP 402 from https://us.store.bambulab.com/agree-terms),
so the earlier wording below is superseded where it differs. The software licence text shown at
install time was not seen.

- Secondhand forum repost (https://groups.google.com/g/3d-printing-tips--tricks/c/PzNhy_lBytk),
  now superseded by the Terms text: a non-exclusive licence "on one Bambu Lab device", no
  commercial use, no modifying or reverse engineering the software.
- Bambu's own blog (https://blog.bambulab.com/setting-the-record-straight-on-cloud-access-and-community/,
  fetched 2026-11-24): "Our cloud is a private service. Access to it is governed by a user
  agreement, not the AGPL license." and "impersonating official clients in communication
  with our cloud infrastructure is not allowed." This concerns the cloud and the plugin,
  which tulda does not touch. Bambu's position on terms overriding AGPL is disputed by SFC.
- Not found in the Terms (see the next section): any clause on CLI automation, undocumented
  flags, sharing rendered previews, or third-party tool naming/trademark. Bambu Lab is a
  trademark; use it descriptively only.

## Bambu Lab Terms of Use (reviewed 2026-11-24, version dated 24 April 2024)

URL: https://bambulab.com/en-us/policies/terms (the version reviewed is marked "last updated
24 April 2024"). Read from the full text supplied for this review. Quotes are short excerpts;
read the Terms themselves before relying on this.

Scope (sections 1.1 and 1.2):
- 1.1 defines "the Product" as "the Bambu Lab device, and the software contained therein",
  including updates.
- 1.2 says the Software is bound "whether it is stored in a read-only memory or any other media
  or form or it is from an online download location authorized by Bambu Lab".
- Open interpretation question (inference): the text does not make clear whether Bambu Studio,
  which is separately licensed under AGPL-3.0 on GitHub, counts as "the Software" here.

Grants and restrictions that bear on tulda:
- 2: "a limited and non-exclusive licence to access and use this software ... on only one Bambu
  Lab device".
- 3.2: "You may not distribute or provide the Software through networks so that it is available
  to multiple devices or computers at the same time."
- 3.4: no "reverse engineer, publish, publicly disseminate, decompile, export codes, disassemble
  or create derivatives of the Product".
- 3.5(2): no "provide to third parties, or allow third parties to use the whole or part of the
  Software without obtaining Bambu Lab's written consent (including but not limited to apps,
  services, code, and source code)".
- 8.2 and the "Bambu cloud" paragraph: cloud and account features (for example remote printing
  and remote monitoring) need an account and "additional terms". tulda uses none of them.
- 17: governing law is the law of the People's Republic of China, with CIETAC arbitration.

Not in this text: no express clause on automating the CLI, headless rendering, trademarks or
branding of third-party tools, or sharing preview images of models. This is an absence in this
text, not a legal conclusion.

### 3. File formats

- 3MF: https://www.3mf.io/ (fetched 2026-11-24): "An open, XML-based standard", ISO/IEC
  25422:2025, specification licence listed as "Free & Open Source", no licensing fees;
  the page states no usage restrictions.
- Bambu extensions (`Metadata/*.config`, `project_settings`) are ordinary entries in the
  3MF zip. I found no published licence or restriction on them in the Bambu Terms (see the
  Terms section above). Parsing a user's own file with stdlib is ordinary interoperability
  (inference). 3MF has an optional "Secure Content" encryption feature; tulda must not try
  to bypass any encrypted/protected content.

### 4. Cloud, plugin, authentication, printers

None touched, by code inspection (see above). Keep it that way: any future feature that
loads the networking plugin, logs in, or talks to a printer or MakerWorld would bring Bambu's
cloud user agreement and "additional terms" into scope (see the Terms section above).

## Conclusions

| Bucket | Verdict | Basis |
|---|---|---|
| (a) Private single-user use on the user's own machine and devices | No clause found that this breaches the Terms. High confidence on AGPL. | Separate process, no bundling; AGPL permits use. Remaining uncertainty: whether the Terms apply to Studio at all (inference: "the Software" is tied to the device, and Studio's AGPL licence is separate); and the undocumented `--camera-view` values. Clause 3.4 is about decompiling and reverse engineering code; probing a CLI flag value is arguably not that (inference). |
| (b) Public repo, each user runs their own installed Studio | Same open questions as (a); no new breach identified | tulda distributes none of the Software, so 3.3 and 3.5(2) are not engaged by distribution of tulda itself (inference). AGPL distribution duties don't arise (no Bambu code shipped). Trademark: descriptive use only, with a non-affiliation notice. |
| (c) Hosted or shared service where third parties use the owner's installed Studio, even indirectly by rendering their models | Unclear, leaning likely a problem | Clauses 2, 3.2 and 3.5(2) make this a real risk (inference): one licensed install serving other people, on more than one device, or to third parties without written consent. AGPL section 13 would matter if Studio were modified. Recommendation: do not do this without Bambu Lab's written consent. |

## Recommendations

1. Use tulda only with your own models and your own licensed Studio install; do not expose
   it publicly or run it as a service for third parties; for third-party use, get Bambu Lab's
   written consent first.
2. Re-read the Terms of Use at https://bambulab.com/en-us/policies/terms (version dated 24 April
   2024 when reviewed) and the licence shown at Studio install. Re-check clauses 2, 3.2, 3.4,
   3.5(2) and 17 if the version changes.
3. Keep tulda away from the networking plugin, cloud, authentication and printers.
4. Treat `--camera-view` 4-7 as unstable; there is a documented fallback plan (OpenSCAD render
   of an extracted mesh) in `docs/PLAN.md`.
5. For the GLB phase, convert only the user's own files, do not bypass 3MF encryption, and do
   not republish converted meshes of third-party models.
6. Keep the non-affiliation notice and use "Bambu Studio" only to name the tool being run.
