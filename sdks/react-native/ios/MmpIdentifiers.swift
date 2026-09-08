import AdSupport
import AppTrackingTransparency
import StoreKit
import Foundation
import UIKit

/// The identifier logic, with no React Native dependency.
///
/// Kept separate from the bridge so it can be compiled and reasoned about on
/// its own — the bridge below is a forwarding shim thin enough to review by
/// eye, and everything with a decision in it lives here.
///
/// Two rules govern this file.
///
/// **The IDFA is never read unless App Tracking Transparency has authorised
/// it.** Not "read and then withheld" — reading it at all without
/// authorisation is what the framework exists to prevent, and Apple rejects
/// apps for it. On an unauthorised device `ASIdentifierManager` returns the
/// all-zero UUID anyway, which is why `advertisingIdentifier` is treated as
/// absent rather than as a value.
///
/// **This never presents the ATT prompt.** The prompt can be shown once per
/// install, and the app decides when — after explaining why, at a moment that
/// makes sense to the person using it. An SDK that triggers it during
/// `initialize` spends that one chance on a cold launch and materially lowers
/// the opt-in rate. `requestTrackingAuthorization` is offered separately and
/// only fires when the app calls it.
@objc public final class MmpIdentifiers: NSObject {

    /// The all-zero UUID both platforms return when tracking is not permitted.
    /// It is not an identifier: sending it would give every opted-out device on
    /// earth the same value, and the server would match them to one another.
    static let zeroUUID = "00000000-0000-0000-0000-000000000000"

    /// The advertising identifier, or `nil`.
    ///
    /// `nil` covers every "no" — not determined, denied, restricted, or an OS
    /// too old to ask — because they are the same answer to the only question
    /// the caller has.
    @objc public static func advertisingIdentifier() -> String? {
        guard isTrackingAuthorized() else { return nil }

        let identifier = ASIdentifierManager.shared().advertisingIdentifier.uuidString
        guard identifier.caseInsensitiveCompare(zeroUUID) != .orderedSame else { return nil }
        return identifier
    }

    @objc public static func isTrackingAuthorized() -> Bool {
        if #available(iOS 14, *) {
            return ATTrackingManager.trackingAuthorizationStatus == .authorized
        }
        return legacyTrackingEnabled()
    }

    /// The pre-iOS-14 switch. Deliberately still here: React Native 0.72
    /// deploys to iOS 13.4, so these devices are reachable, and assuming
    /// "allowed" on them would read the identifier of someone who had turned it
    /// off. The annotation scopes the deprecation to this one function instead
    /// of leaving a warning in the build for everyone who compiles the SDK.
    @available(iOS, introduced: 6.0, deprecated: 14.0, message: "Superseded by ATTrackingManager")
    private static func legacyTrackingEnabled() -> Bool {
        return ASIdentifierManager.shared().isAdvertisingTrackingEnabled
    }

    /// Presents the ATT prompt. Only ever called by the host app, never by the
    /// SDK's own initialisation — see the note on this type.
    @objc public static func requestTrackingAuthorization(
        completion: @escaping (Bool) -> Void
    ) {
        guard #available(iOS 14, *) else {
            completion(ASIdentifierManager.shared().isAdvertisingTrackingEnabled)
            return
        }
        ATTrackingManager.requestTrackingAuthorization { status in
            // Back to the main queue: the callback arrives on an arbitrary one
            // and the app will almost certainly touch its UI from here.
            DispatchQueue.main.async { completion(status == .authorized) }
        }
    }

    @objc public static func deviceInfo() -> [String: String] {
        var info: [String: String] = ["platform": "ios"]
        info["osVersion"] = UIDevice.current.systemVersion
        info["deviceModel"] = hardwareModel()
        if let version = Bundle.main.infoDictionary?["CFBundleShortVersionString"] as? String {
            info["appVersion"] = version
        }
        return info
    }

    /// The hardware identifier — "iPhone15,3" — rather than
    /// `UIDevice.current.model`, which returns "iPhone" for every iPhone ever
    /// made and is useless for the device breakdown this feeds.
    private static func hardwareModel() -> String {
        var systemInfo = utsname()
        uname(&systemInfo)
        let mirror = Mirror(reflecting: systemInfo.machine)
        return mirror.children.reduce(into: "") { identifier, element in
            guard let value = element.value as? Int8, value != 0 else { return }
            identifier += String(UnicodeScalar(UInt8(value)))
        }
    }

    /// Reports a conversion value to SKAdNetwork.
    ///
    /// Three APIs across three iOS versions, and the differences matter:
    /// the coarse value only exists from 16.1, and before 15.4 there is no
    /// completion handler at all. Calling the newest one unconditionally would
    /// crash on anything older, so each tier is entered explicitly.
    ///
    /// Apple *ignores* a decrease rather than reporting one, and every accepted
    /// call restarts the measurement window. Deciding whether a call is worth
    /// making therefore happens before this point — see `conversion.ts` — and
    /// this method only sends what it is given.
    @objc public static func updateConversionValue(
        fineValue: Int,
        coarseValue: String?,
        completion: @escaping (Bool) -> Void
    ) {
        if #available(iOS 16.1, *) {
            SKAdNetwork.updatePostbackConversionValue(
                fineValue,
                coarseValue: coarse(from: coarseValue)
            ) { error in
                completion(error == nil)
            }
        } else if #available(iOS 15.4, *) {
            // No coarse value before 16.1; the fine value is all there is.
            SKAdNetwork.updatePostbackConversionValue(fineValue) { error in
                completion(error == nil)
            }
        } else if #available(iOS 14, *) {
            // Fire and forget: this API reports nothing back.
            SKAdNetwork.updateConversionValue(fineValue)
            completion(true)
        } else {
            completion(false)
        }
    }

    @available(iOS 16.1, *)
    private static func coarse(from value: String?) -> SKAdNetwork.CoarseConversionValue {
        switch value {
        case "high": return .high
        case "medium": return .medium
        case "low": return .low
        // Apple has no "unset" case, and `.low` is the honest default: claiming
        // a higher tier than the app asked for would overstate the conversion.
        default: return .low
        }
    }

    /// Apple provides no install-referrer equivalent, so there is nothing to
    /// return. This exists so the bridge has the same shape on both platforms
    /// and the JavaScript side needs no per-platform branch.
    ///
    /// It is also the reason iOS attribution needs SKAdNetwork: there is no
    /// channel that carries a click identifier through an App Store install.
    @objc public static func installReferrer() -> String? {
        return nil
    }
}
