INGENIC_UCLIBC_VERSION = 97c9ba8
INGENIC_UCLIBC_SITE = https://github.com/gtxaspec/ingenic-uclibc
INGENIC_UCLIBC_SITE_METHOD = git
INGENIC_UCLIBC_INSTALL_STAGING = YES

INGENIC_UCLIBC_LICENSE = MIT
INGENIC_UCLIBC_LICENSE_FILES = LICENSE

# ---------------------------------------------------------------------------
# PINNED PREBUILT libuclibcshim.so  (root-cause fix for the PT2 startup crash)
# ---------------------------------------------------------------------------
# libuclibcshim.so is a hand-written glibc/musl->uClibc ABI bridge (uclibc_shim.c)
# that pokes raw FILE-struct offsets and interposes libc symbols. Compiling it from
# source with the buildroot 2026.02.1 gcc (especially with -flto) MISCOMPILED it:
# prudynt SIGSEGV'd at startup inside the shim's __fputc_unlocked (epc libuclibcshim
# +0xbc8) and EVERY prudynt crashed -- this was the real root cause, never prudynt
# or the daynight/0015 work. The bad rebuild's sha was b3ead471...; the proven-good
# binary (built by the older toolchain, sha 07710f80...) was verified to boot prudynt
# on the new 2026.02.1 rootfs (swapped onto the camera).
#
# Until the toolchain miscompile is fixed upstream, we SHIP THE PINNED PROVEN-GOOD .so
# (committed at prebuilt/libuclibcshim.so) instead of risking a bad rebuild. The build
# verifies its sha and refuses to proceed if the pinned file was altered. The static
# archive (.a, used only when raptor links the shim statically) is still built from
# source; PT2/prudynt use the dynamic .so. If you bump buildroot or the shim source,
# rebuild the .so, verify prudynt boots, then re-pin (update the file AND the sha below).
INGENIC_UCLIBC_GOOD_SHA = 07710f80ebc4683c9a9c658142a3cdce018db2949b844647e1709446e21c369b
INGENIC_UCLIBC_PREBUILT = $(INGENIC_UCLIBC_PKGDIR)/prebuilt/libuclibcshim.so

# -flto deliberately omitted (it is what miscompiled the .so); kept off for the .a too.
INGENIC_UCLIBC_CFLAGS = -Os -ffunction-sections -fdata-sections \
	-fno-asynchronous-unwind-tables -fmerge-all-constants -fno-ident

define INGENIC_UCLIBC_BUILD_CMDS
	# Fail hard if the pinned, proven-good .so was tampered with / corrupted.
	echo "$(INGENIC_UCLIBC_GOOD_SHA)  $(INGENIC_UCLIBC_PREBUILT)" | sha256sum -c -
	# Static archive still built from source (raptor links it statically).
	$(TARGET_CC) $(INGENIC_UCLIBC_CFLAGS) -c -o $(@D)/uclibc_shim.o $(@D)/uclibc_shim.c
	$(TARGET_CROSS)gcc-ar rcs $(@D)/libuclibcshim.a $(@D)/uclibc_shim.o
endef

define INGENIC_UCLIBC_INSTALL_STAGING_CMDS
	$(INSTALL) -D -m 0755 $(INGENIC_UCLIBC_PREBUILT) $(STAGING_DIR)/usr/lib/libuclibcshim.so
	$(INSTALL) -D -m 0644 $(@D)/libuclibcshim.a $(STAGING_DIR)/usr/lib/libuclibcshim.a
endef

# Raptor links the shim statically — skip .so on device when raptor is the streamer
ifneq ($(BR2_PACKAGE_THINGINO_RAPTOR),y)
define INGENIC_UCLIBC_INSTALL_TARGET_CMDS
	$(INSTALL) -D -m 0755 $(INGENIC_UCLIBC_PREBUILT) $(TARGET_DIR)/usr/lib/libuclibcshim.so
endef
endif

$(eval $(generic-package))
