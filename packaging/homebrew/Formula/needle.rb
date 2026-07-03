# frozen_string_literal: true

# Pi-first local observation pruning runtime.
class Needle < Formula
  desc "Pi-first local observation pruning runtime"
  homepage "https://github.com/e24z/needle"
  url "https://github.com/e24z/needle.git", branch: "push-1.0"
  license "MIT"

  depends_on "rust" => :build
  depends_on "python@3.13"

  def install
    system "cargo", "install", *std_cargo_args(path: "crates/needle-manager")
    (share/"needle/python").install Dir["python/*"]
    (share/"needle/pi").install Dir["pi/*"]
  end

  test do
    assert_match "needle: off", shell_output("#{bin}/needle status")
  end
end
