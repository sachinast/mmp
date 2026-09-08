import Foundation

/// A command-line harness that runs `MmpIdentifiers` on a real simulator or
/// device and prints what it actually returns.
///
/// This exists because typechecking proved the code compiles and proved nothing
/// about what it does. Several of the behaviours that matter here — an IDFA of
/// all zeros before ATT authorisation, a hardware model string that is not
/// "iPhone", the absence of any install referrer on iOS — can only be seen by
/// running it.
///
/// It is not a unit test. It prints observations and asserts the few invariants
/// that must hold on any device, so a person can read the rest and judge them
/// against the machine it ran on. Run it with `make sdk-ios-device`.
func report(_ label: String, _ value: Any?) {
    let shown: String
    if let value = value as? String, value.isEmpty {
        shown = "<empty>"
    } else if let value {
        shown = "\(value)"
    } else {
        shown = "nil"
    }
    print("  \(label.padding(toLength: 26, withPad: " ", startingAt: 0)) \(shown)")
}

var failures: [String] = []

func expect(_ condition: Bool, _ description: String) {
    if condition {
        print("  ok    \(description)")
    } else {
        print("  FAIL  \(description)")
        failures.append(description)
    }
}

print("MmpIdentifiers, on this device")
print("")

print("tracking authorisation")
let authorised = MmpIdentifiers.isTrackingAuthorized()
report("isTrackingAuthorized", authorised)

print("")
print("advertising identifier")
let identifier = MmpIdentifiers.advertisingIdentifier()
report("advertisingIdentifier", identifier)

// The invariant that matters most, and the one a simulator can genuinely
// prove: with ATT unauthorised, nothing may come back. A simulator with no
// prompt answered is exactly the unauthorised case.
expect(
    authorised || identifier == nil,
    "no advertising identifier is returned while tracking is unauthorised"
)
// And the all-zero UUID must never escape as if it were an identifier, which
// is what an opted-out device actually returns underneath.
expect(
    identifier != MmpIdentifiers.zeroUUID,
    "the all-zero identifier is never returned as a value"
)

print("")
print("install referrer")
let referrer = MmpIdentifiers.installReferrer()
report("installReferrer", referrer)
expect(referrer == nil, "iOS has no install-referrer equivalent, so this is always nil")

print("")
print("device info")
let info = MmpIdentifiers.deviceInfo()
for key in info.keys.sorted() {
    report(key, info[key])
}
expect(info["platform"] == "ios", "platform is reported as ios")
expect(!(info["osVersion"] ?? "").isEmpty, "an OS version is reported")
// UIDevice.current.model returns "iPhone" for every iPhone ever made, which is
// useless for a device breakdown. The hardware identifier is the useful one.
// A hardware identifier always contains a comma — "iPhone17,3", "iPad14,6".
// The check is deliberately strict: an earlier version accepted anything
// starting with "arm", which let the simulator's useless "arm64" pass as if it
// were a device model.
expect(
    (info["deviceModel"] ?? "").contains(","),
    "deviceModel is a hardware identifier (iPhone17,3), not the host architecture"
)

print("")
print("conversion value")
let semaphore = DispatchSemaphore(value: 0)
var conversionAccepted: Bool?
MmpIdentifiers.updateConversionValue(fineValue: 12, coarseValue: "medium") { ok in
    conversionAccepted = ok
    semaphore.signal()
}
_ = semaphore.wait(timeout: .now() + 10)
report("updateConversionValue(12)", conversionAccepted.map(String.init) ?? "timed out")
// Not asserted: a simulator has no SKAdNetwork backend, so a false here is
// expected and says nothing about a real device. It is printed so a person
// running this on hardware can see the difference.

print("")
if failures.isEmpty {
    print("all invariants held")
    exit(0)
} else {
    print("\(failures.count) invariant(s) failed:")
    for failure in failures { print("  - \(failure)") }
    exit(1)
}
