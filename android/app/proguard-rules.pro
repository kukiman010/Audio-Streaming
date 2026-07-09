# LiveKit / WebRTC
-keep class io.livekit.** { *; }
-keep class org.webrtc.** { *; }
-dontwarn org.webrtc.**

# AudioSwitch (davidliu fork required by LiveKit)
-keep class com.twilio.audioswitch.** { *; }

# Kotlin coroutines
-dontwarn kotlinx.coroutines.**
-keep class kotlinx.coroutines.** { *; }
-keepclassmembers class kotlinx.coroutines.** { *; }

# Media session / ML Kit / CameraX
-keep class androidx.media.** { *; }
-dontwarn com.google.mlkit.**
-dontwarn androidx.camera.**
