# kotlinx.serialization
-keepattributes *Annotation*, InnerClasses
-dontnote kotlinx.serialization.AnnotationsKt
-keepclassmembers class kotlinx.serialization.json.** { *** Companion; }
-keepclasseswithmembers class kotlinx.serialization.json.** {
    kotlinx.serialization.KSerializer serializer(...);
}
-keep,includedescriptorclasses class com.appagentcards.data.model.**$$serializer { *; }
-keepclassmembers class com.appagentcards.data.model.** {
    *** Companion;
}

# kaml
-keep class kotlinx.serialization.** { *; }
