# three.js 0.187.0 (vendored, unmodified)

- Upstream: https://github.com/mrdoob/three.js, npm package `three@0.187.0`
- Licence: MIT (see `LICENSE` next to this file)
- Fetched from the npm registry tarball `https://registry.npmjs.org/three/-/three-0.187.0.tgz`
  with plain `curl`; extracted with `tar` into an empty directory. No install or lifecycle
  scripts were run.
- Tarball integrity, verified against the registry's `dist.integrity` and `dist.shasum`:
  - sha1   `face29ee2de9bcc31a1b573a76f224e35e0b2894`
  - sha512 `Qiu7R+Cmh8CstZsY1uJOSWnVlfo7ZtNLV7mF8Jltk3LB3AtDu9cRZlcnRKkAk8Y4z6KKw35z7Y7Urd7jWY8etA==`
  - sha256 `ff540832ded0cfe919764d24a3d88f3e2a05f23303f876423e4f893841e90728`

Files (copied byte-for-byte from the tarball's `package/` directory; same relative layout so
the ES module imports resolve; the bare specifier `three` is mapped by an import map in the
viewer page):

| File | sha256 |
|---|---|
| `build/three.module.js` | `104f56145899722094556490d787aae0617cc2fc3382f31df02d724ea1aee341` |
| `build/three.core.js` | `613e370f76638fdef83926642bf256dc9615684922de1ce1f63a8583e5d78578` |
| `examples/jsm/controls/OrbitControls.js` | `e7fca2fe9e7670a828af9b28084669a5909445b7f36f1f26cee9bf8f2961c32c` |
| `examples/jsm/loaders/GLTFLoader.js` | `532ac726e120039acbcd6cf54328c57acbf7b438176afaa6951257126d11bff1` |
| `examples/jsm/utils/BufferGeometryUtils.js` | `7bda2a8ba836b2bcf67cb938d4b9e1bb5d1b1bc616817103ed858c0b58296ce7` |
| `examples/jsm/utils/SkeletonUtils.js` | `b1632a703206c3d830de9fcbe515696770d04b71a15ee6b50afa6d2c3298c86f` |
| `LICENSE` | `8b378ebe60e2fe500158cb0ac71cb5e8b7d92953c2abcc63a0eb90499653b5bc` |

To upgrade: repeat the download into a fresh directory, verify the integrity against
`npm view three@X dist.integrity`, copy the same files into a new `three-X/` directory, update
the import map and `STATIC_FILES` in `tulda/server.py`, and this file. The versioned directory
name is what makes the `immutable` cache header safe.
