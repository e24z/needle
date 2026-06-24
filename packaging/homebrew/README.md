# Homebrew Packaging

This directory is the source-repo copy of the Homebrew packaging recipe.

The actual public tap should be a separate repository such as
`e24z/homebrew-tap`. Copy `Formula/needle.rb` there when cutting a release and
replace the placeholder SHA256 with the release tarball hash.

Expected public install:

```bash
brew install e24z/tap/needle
needle setup pi
```
