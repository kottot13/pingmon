# Homebrew formula for pingmon (macOS + Linuxbrew).
#
# Two ways to use it:
#   1. Personal tap:  put this file in a tap repo `homebrew-tap/Formula/pingmon.rb`
#      then:  brew install kottot13/tap/pingmon
#   2. Local test:     brew install --build-from-source ./packaging/pingmon.rb
#
# Before publishing: release pingmon to PyPI, then set `url`/`sha256` to the
# sdist and run `brew update-python-resources Formula/pingmon.rb` to auto-fill
# the `resource` blocks for textual and its dependencies.
class Pingmon < Formula
  include Language::Python::Virtualenv

  desc "TUI monitor of latency and availability to servers by country"
  homepage "https://github.com/kottot13/pingmon"
  url "https://files.pythonhosted.org/packages/source/p/pingmon/pingmon-1.0.0.tar.gz"
  sha256 "REPLACE_WITH_SDIST_SHA256"
  license "MIT"

  depends_on "python@3.12"

  # `brew update-python-resources` fills these in automatically:
  # resource "textual" do ... end
  # resource "rich" do ... end
  # ...

  def install
    virtualenv_install_with_resources
  end

  test do
    assert_match "pingmon #{version}", shell_output("#{bin}/pingmon --version")
  end
end
