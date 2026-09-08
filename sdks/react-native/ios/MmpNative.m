#import <React/RCTBridgeModule.h>

// Registers the Swift class above with React Native. Required because the
// bridge's module registry is discovered from Objective-C macros, which Swift
// cannot emit.
@interface RCT_EXTERN_MODULE (MmpNative, NSObject)

RCT_EXTERN_METHOD(getAdvertisingId
                  : (RCTPromiseResolveBlock)resolve rejecter
                  : (RCTPromiseRejectBlock)reject)

RCT_EXTERN_METHOD(getInstallReferrer
                  : (RCTPromiseResolveBlock)resolve rejecter
                  : (RCTPromiseRejectBlock)reject)

RCT_EXTERN_METHOD(getDeviceInfo
                  : (RCTPromiseResolveBlock)resolve rejecter
                  : (RCTPromiseRejectBlock)reject)

RCT_EXTERN_METHOD(updateConversionValue
                  : (nonnull NSNumber *)fineValue coarseValue
                  : (nullable NSString *)coarseValue resolver
                  : (RCTPromiseResolveBlock)resolve rejecter
                  : (RCTPromiseRejectBlock)reject)

RCT_EXTERN_METHOD(requestTrackingAuthorization
                  : (RCTPromiseResolveBlock)resolve rejecter
                  : (RCTPromiseRejectBlock)reject)

@end
