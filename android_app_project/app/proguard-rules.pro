# Regras específicas do aplicativo. O release atual mantém minificação desativada.
# O arquivo existe para builds reproduzíveis e futura ativação segura do R8.
-keepattributes *Annotation*
-dontwarn com.google.zxing.**
