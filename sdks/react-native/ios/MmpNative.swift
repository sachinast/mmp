import Foundation

/// The React Native bridge. A forwarding shim and nothing else — every decision
/// lives in `MmpIdentifiers`, which compiles without React and can therefore be
/// typechecked and reasoned about on its own.
///
/// Written against the classic bridge API. It runs unchanged under the New
/// Architecture through the interop layer; migrating to a TurboModule means
/// adding a codegen spec, not rewriting this.
@objc(MmpNative)
final class MmpNative: NSObject {

    /// Nothing here touches UIKit on the main thread except the ATT prompt,
    /// which dispatches itself. Returning false keeps these calls off the main
    /// queue so a cold launch is not waiting on them.
    @objc static func requiresMainQueueSetup() -> Bool {
        return false
    }

    @objc(getAdvertisingId:rejecter:)
    func getAdvertisingId(
        resolve: @escaping (Any?) -> Void,
        reject: @escaping (String?, String?, Error?) -> Void
    ) {
        // Resolved with nil rather than rejected when there is no identifier.
        // "The user said no" is an answer, not a failure, and rejecting would
        // put an error in the app's console for a completely normal state.
        resolve(MmpIdentifiers.advertisingIdentifier())
    }

    @objc(getInstallReferrer:rejecter:)
    func getInstallReferrer(
        resolve: @escaping (Any?) -> Void,
        reject: @escaping (String?, String?, Error?) -> Void
    ) {
        resolve(MmpIdentifiers.installReferrer())
    }

    @objc(getDeviceInfo:rejecter:)
    func getDeviceInfo(
        resolve: @escaping (Any?) -> Void,
        reject: @escaping (String?, String?, Error?) -> Void
    ) {
        resolve(MmpIdentifiers.deviceInfo())
    }

    /// Presents the ATT prompt. Exposed so the *app* can choose the moment —
    /// the SDK never calls this itself. See `MmpIdentifiers`.
    @objc(requestTrackingAuthorization:rejecter:)
    func requestTrackingAuthorization(
        resolve: @escaping (Any?) -> Void,
        reject: @escaping (String?, String?, Error?) -> Void
    ) {
        MmpIdentifiers.requestTrackingAuthorization { authorized in
            resolve(authorized)
        }
    }
}
