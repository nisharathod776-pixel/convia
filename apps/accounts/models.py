from django.contrib.auth.models import AbstractUser
from django.db import models


class User(AbstractUser):
    ROLE_MEMBER = "member"
    ROLE_ADMIN = "admin"
    ROLE_CHOICES = [
        (ROLE_MEMBER, "Member"),
        (ROLE_ADMIN, "Admin"),
    ]

    email = models.EmailField(unique=True)
    full_name = models.CharField(max_length=120, blank=True)
    bio = models.TextField(blank=True)
    role = models.CharField(max_length=20, choices=ROLE_CHOICES, default=ROLE_MEMBER)

    def save(self, *args, **kwargs):
        if self.is_superuser or self.is_staff:
            self.role = self.ROLE_ADMIN
        elif self.role == self.ROLE_ADMIN:
            self.is_staff = True
        super().save(*args, **kwargs)

    def __str__(self):
        return self.username
