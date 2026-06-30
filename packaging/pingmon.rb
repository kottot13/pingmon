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
  url "https://files.pythonhosted.org/packages/4d/38/c6f6b59319d39d4d66de657ff39a4be41515e685fdd3b17ac981f202b43d/pingmonitor-1.0.0.tar.gz"
  sha256 "702643a16ab4b012cdc65950caa98261f58ea0cd0e9630e7e23683fcb228be85"
  license "MIT"

  depends_on "python@3.12"

  # `brew update-python-resources` fills these in automatically:
  # resource "textual" do ... end
  # resource "pyte" do ... end   # drives the embedded SSH / top terminals
  # resource "rich" do ... end
  # ...

  def install
    virtualenv_install_with_resources
  end

  test do
    assert_match "pingmon #{version}", shell_output("#{bin}/pingmon --version")
  end
end
