require "json"

package = JSON.parse(File.read(File.join(__dir__, "package.json")))

Pod::Spec.new do |s|
  s.name         = "mmp-react-native"
  s.version      = package["version"]
  s.summary      = package["description"]
  s.license      = package["license"]
  s.authors      = { "MMP" => "platform@example.com" }
  s.homepage     = "https://example.com/mmp"
  s.platforms    = { :ios => "13.4" }
  s.source       = { :path => "." }
  s.source_files = "ios/**/*.{h,m,mm,swift}"

  # AdSupport and AppTrackingTransparency are weak-linked: an app that never
  # asks for the advertising identifier should not have to carry the frameworks,
  # and on a device where they are unavailable the calls resolve to nil rather
  # than failing to launch.
  s.weak_frameworks = ["AdSupport", "AppTrackingTransparency"]

  s.dependency "React-Core"
end
