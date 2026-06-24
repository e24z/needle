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

Before the first release tag, smoke the development branch by copying the
formula into a throwaway local tap. Homebrew 6 rejects formula files that are not
inside a tap.

```bash
brew tap-new --no-git e24z/needle-local
cp packaging/homebrew/Formula/needle.rb "$(brew --repository e24z/needle-local)/Formula/needle.rb"
brew install --build-from-source --HEAD e24z/needle-local/needle
brew test e24z/needle-local/needle
brew uninstall needle
brew untap e24z/needle-local
```

This build path uses Homebrew's Python toolchain. If it reports outdated macOS
Command Line Tools, update CLT before treating the formula as broken.
