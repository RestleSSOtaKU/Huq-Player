#import <Foundation/Foundation.h>
#import <CoreAudio/CoreAudio.h>
#import <CoreAudio/AudioHardwareTapping.h>
#import <CoreAudio/CATapDescription.h>
#include <stdatomic.h>
#include <string.h>
#include <unistd.h>

#define TAP_RING_CAP 131072
static float gTapRing[TAP_RING_CAP * 2];
static _Atomic uint64_t gTapWrite = 0;
static _Atomic uint64_t gTapRead = 0;
static AudioObjectID gTapID = kAudioObjectUnknown;
static AudioObjectID gAggID = kAudioObjectUnknown;
static AudioDeviceIOProcID gProcID = NULL;
static double gTapRate = 48000.0;

static OSStatus TapIOProc(AudioObjectID inDevice, const AudioTimeStamp* inNow,
                          const AudioBufferList* inInputData, const AudioTimeStamp* inInputTime,
                          AudioBufferList* outOutputData, const AudioTimeStamp* inOutputTime,
                          void* inClientData) {
    (void)inDevice; (void)inNow; (void)inInputTime; (void)outOutputData; (void)inOutputTime; (void)inClientData;
    if (!inInputData || inInputData->mNumberBuffers < 1) return noErr;
    const AudioBuffer* buf = &inInputData->mBuffers[0];
    if (!buf->mData || buf->mDataByteSize == 0) return noErr;
    const float* samples = (const float*)buf->mData;
    uint32_t ch = buf->mNumberChannels > 0 ? buf->mNumberChannels : 2;
    uint32_t frames = buf->mDataByteSize / (sizeof(float) * ch);
    uint64_t w = atomic_load(&gTapWrite);
    for (uint32_t i = 0; i < frames; i++) {
        uint64_t idx = ((w + i) % TAP_RING_CAP) * 2;
        gTapRing[idx] = samples[i * ch];
        gTapRing[idx + 1] = (ch > 1) ? samples[i * ch + 1] : samples[i * ch];
    }
    atomic_store(&gTapWrite, w + frames);
    return noErr;
}

void huq_tap_stop(void) {
    if (gAggID != kAudioObjectUnknown) {
        if (gProcID) { AudioDeviceStop(gAggID, gProcID); AudioDeviceDestroyIOProcID(gAggID, gProcID); gProcID = NULL; }
        AudioHardwareDestroyAggregateDevice(gAggID);
        gAggID = kAudioObjectUnknown;
    }
    if (gTapID != kAudioObjectUnknown) {
        AudioHardwareDestroyProcessTap(gTapID);
        gTapID = kAudioObjectUnknown;
    }
}

double huq_tap_start(void) {
    huq_tap_stop();
    atomic_store(&gTapWrite, 0);
    atomic_store(&gTapRead, 0);
    memset(gTapRing, 0, sizeof(gTapRing));

    @autoreleasepool {
        AudioObjectID selfObj = kAudioObjectUnknown;
        pid_t pid = getpid();
        AudioObjectPropertyAddress addr = { kAudioHardwarePropertyTranslatePIDToProcessObject, kAudioObjectPropertyScopeGlobal, kAudioObjectPropertyElementMain };
        UInt32 sz = sizeof(selfObj);
        NSMutableArray<NSNumber*>* excl = [NSMutableArray array];
        if (AudioObjectGetPropertyData(kAudioObjectSystemObject, &addr, sizeof(pid), &pid, &sz, &selfObj) == noErr && selfObj != kAudioObjectUnknown) {
            [excl addObject:@(selfObj)];
        }
        CATapDescription* desc = [[CATapDescription alloc] initStereoGlobalTapButExcludeProcesses:excl];
        desc.name = @"HuqSafeTap";
        desc.privateTap = YES;
        desc.muteBehavior = CATapMuted;
        if (AudioHardwareCreateProcessTap(desc, &gTapID) != noErr || gTapID == kAudioObjectUnknown) return 0.0;

        NSString* tapUID = nil;
        AudioObjectPropertyAddress uAddr = { kAudioTapPropertyUID, kAudioObjectPropertyScopeGlobal, kAudioObjectPropertyElementMain };
        UInt32 uSz = sizeof(CFStringRef);
        CFStringRef cfUID = NULL;
        if (AudioObjectGetPropertyData(gTapID, &uAddr, 0, NULL, &uSz, &cfUID) == noErr && cfUID) {
            tapUID = (__bridge_transfer NSString*)cfUID;
        }
        if (!tapUID) { huq_tap_stop(); return 0.0; }

        NSDictionary* aggDesc = @{
            @kAudioAggregateDeviceNameKey: @"HuqTapAgg",
            @kAudioAggregateDeviceUIDKey: [[NSUUID UUID] UUIDString],
            @kAudioAggregateDeviceIsPrivateKey: @YES,
            @kAudioAggregateDeviceTapListKey: @[ @{ @kAudioSubTapUIDKey: tapUID } ],
            @kAudioAggregateDeviceTapAutoStartKey: @YES
        };
        if (AudioHardwareCreateAggregateDevice((__bridge CFDictionaryRef)aggDesc, &gAggID) != noErr || gAggID == kAudioObjectUnknown) {
            huq_tap_stop(); return 0.0;
        }

        Float64 sr = 48000.0;
        UInt32 srSz = sizeof(sr);
        AudioObjectPropertyAddress srAddr = { kAudioDevicePropertyNominalSampleRate, kAudioObjectPropertyScopeGlobal, kAudioObjectPropertyElementMain };
        AudioObjectGetPropertyData(gAggID, &srAddr, 0, NULL, &srSz, &sr);
        gTapRate = sr > 0 ? sr : 48000.0;

        if (AudioDeviceCreateIOProcID(gAggID, TapIOProc, NULL, &gProcID) != noErr || AudioDeviceStart(gAggID, gProcID) != noErr) {
            huq_tap_stop(); return 0.0;
        }
        return gTapRate;
    }
}

uint32_t huq_tap_read(float* out_interleaved, uint32_t max_frames) {
    uint64_t w = atomic_load(&gTapWrite);
    uint64_t r = atomic_load(&gTapRead);
    if (w <= r) return 0;
    uint64_t avail = w - r;
    if (avail > 16384) { r = w - 8192; avail = 8192; }
    uint32_t n = (avail < max_frames) ? (uint32_t)avail : max_frames;
    for (uint32_t i = 0; i < n; i++) {
        uint64_t idx = ((r + i) % TAP_RING_CAP) * 2;
        out_interleaved[i * 2] = gTapRing[idx];
        out_interleaved[i * 2 + 1] = gTapRing[idx + 1];
    }
    atomic_store(&gTapRead, r + n);
    return n;
}
