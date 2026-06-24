# Homebrew Packaging

This directory is the source-repo copy of the Homebrew packaging recipe.

The actual public tap should be a separate repository such as
`e24z/homebrew-tap`. Copy `Formula/needle.rb` there when cutting a release and
replace the placeholder SHA256 with the release tarball hash.

Expected public install:

```bash
brew install e24z/tap/needle
```

The formula calls `needle setup --from-homebrew` in `post_install`. If Homebrew
cannot run the interactive wizard, the formula caveats tell the user to resume
with:

```bash
needle setup
```

Before the first release tag, smoke the development branch with the formula's
HEAD path:

```bash
brew install --build-from-source --HEAD ./packaging/homebrew/Formula/needle.rb
```
