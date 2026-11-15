import re
from django.contrib.auth import authenticate, get_user_model
from django.contrib.auth.password_validation import validate_password
from rest_framework import serializers

User = get_user_model()
USERNAME_REGEX = re.compile(r"^[a-zA-Z0-9_]{3,30}$")


class UserSerializer(serializers.ModelSerializer):
    class Meta:
        model = User
        fields = (
            "id",
            "username",
            "email",
            "full_name",
            "bio",
            "role",
            "is_active",
            "is_staff",
            "date_joined",
        )
        read_only_fields = ("id", "username", "role", "is_active", "is_staff", "date_joined")


class RegisterSerializer(serializers.Serializer):
    full_name = serializers.CharField(max_length=120, min_length=2)
    username = serializers.CharField(max_length=30, min_length=3)
    email = serializers.EmailField()
    password = serializers.CharField(write_only=True, min_length=8)
    password_confirm = serializers.CharField(write_only=True, min_length=8)

    def validate_username(self, value):
        cleaned = value.strip()
        if not USERNAME_REGEX.match(cleaned):
            raise serializers.ValidationError("Username must be 3-30 letters, numbers, or underscores.")
        if User.objects.filter(username__iexact=cleaned).exists():
            raise serializers.ValidationError("An account with this username already exists.")
        return cleaned

    def validate_email(self, value):
        cleaned = value.strip().lower()
        if User.objects.filter(email__iexact=cleaned).exists():
            raise serializers.ValidationError("An account with this email already exists.")
        return cleaned

    def validate(self, attrs):
        if attrs["password"] != attrs["password_confirm"]:
            raise serializers.ValidationError({"password_confirm": "Passwords do not match."})
        validate_password(attrs["password"])
        return attrs

    def create(self, validated_data):
        user = User.objects.create_user(
            username=validated_data["username"],
            email=validated_data["email"],
            password=validated_data["password"],
            full_name=validated_data["full_name"].strip(),
        )
        return user


class LoginSerializer(serializers.Serializer):
    identifier = serializers.CharField()
    password = serializers.CharField(write_only=True)

    def validate(self, attrs):
        identifier = attrs["identifier"].strip()
        password = attrs["password"]

        user_obj = (
            User.objects.filter(email__iexact=identifier).first()
            if "@" in identifier
            else User.objects.filter(username__iexact=identifier).first()
        )
        if not user_obj:
            raise serializers.ValidationError("Invalid username/email or password.")
        if not user_obj.is_active:
            raise serializers.ValidationError("This account has been deactivated. Contact an administrator.")

        user = authenticate(username=user_obj.username, password=password)
        if not user:
            raise serializers.ValidationError("Invalid username/email or password.")

        attrs["user"] = user
        return attrs


class ProfileUpdateSerializer(serializers.ModelSerializer):
    class Meta:
        model = User
        fields = ("full_name", "email", "bio")

    def validate_email(self, value):
        cleaned = value.strip().lower()
        qs = User.objects.filter(email__iexact=cleaned).exclude(pk=self.instance.pk)
        if qs.exists():
            raise serializers.ValidationError("This email address is already in use.")
        return cleaned


class PasswordChangeSerializer(serializers.Serializer):
    old_password = serializers.CharField(write_only=True)
    new_password = serializers.CharField(write_only=True, min_length=8)

    def validate_old_password(self, value):
        user = self.context["request"].user
        if not user.check_password(value):
            raise serializers.ValidationError("Current password is incorrect.")
        return value

    def validate_new_password(self, value):
        validate_password(value, self.context["request"].user)
        return value
