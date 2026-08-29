using System;
using System.ComponentModel;
using System.Collections.Generic;
using System.IO;
using System.Runtime.InteropServices;
using System.Security.Cryptography;
using Microsoft.Win32.SafeHandles;

public static class TownLightCargoArtifactRuntimePublisher
{
    private const uint GenericRead = 0x80000000;
    private const uint GenericWrite = 0x40000000;
    private const uint Delete = 0x00010000;
    private const uint Synchronize = 0x00100000;
    private const uint FileReadAttributes = 0x00000080;
    private const uint FileTraverse = 0x00000020;
    private const uint FileShareRead = 0x00000001;
    private const uint FileShareWrite = 0x00000002;
    private const uint OpenExisting = 3;
    private const uint FileAttributeNormal = 0x00000080;
    private const uint FileAttributeDirectory = 0x00000010;
    private const uint FileAttributeReparsePoint = 0x00000400;
    private const uint FileFlagBackupSemantics = 0x02000000;
    private const uint FileFlagOpenReparsePoint = 0x00200000;
    private const uint FileCreate = 2;
    private const uint FileOpen = 1;
    private const uint FileNonDirectoryFile = 0x00000040;
    private const uint FileDirectoryFile = 0x00000001;
    private const uint FileSynchronousIoNonAlert = 0x00000020;
    private const uint StatusSuccess = 0;
    private const uint StatusObjectNameNotFound = 0xC0000034;
    private const int FileRenameInformation = 10;
    private const int FileDispositionInformation = 13;
    private const uint ObjectCaseInsensitive = 0x00000040;

    private sealed class PinnedDirectoryAncestry : IDisposable
    {
        private readonly List<IntPtr> handles;
        public IntPtr Final { get { return handles[handles.Count - 1]; } }

        public PinnedDirectoryAncestry(List<IntPtr> handles)
        {
            this.handles = handles;
        }

        public void Dispose()
        {
            for (int index = handles.Count - 1; index >= 0; index--)
            {
                CloseHandle(handles[index]);
            }
            handles.Clear();
        }
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct UnicodeString
    {
        public ushort Length;
        public ushort MaximumLength;
        public IntPtr Buffer;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct ObjectAttributes
    {
        public uint Length;
        public IntPtr RootDirectory;
        public IntPtr ObjectName;
        public uint Attributes;
        public IntPtr SecurityDescriptor;
        public IntPtr SecurityQualityOfService;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct IoStatusBlock
    {
        public IntPtr Status;
        public UIntPtr Information;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct ByHandleFileInformation
    {
        public uint FileAttributes;
        public System.Runtime.InteropServices.ComTypes.FILETIME CreationTime;
        public System.Runtime.InteropServices.ComTypes.FILETIME LastAccessTime;
        public System.Runtime.InteropServices.ComTypes.FILETIME LastWriteTime;
        public uint VolumeSerialNumber;
        public uint FileSizeHigh;
        public uint FileSizeLow;
        public uint NumberOfLinks;
        public uint FileIndexHigh;
        public uint FileIndexLow;
    }

    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern IntPtr CreateFileW(
        string fileName,
        uint desiredAccess,
        uint shareMode,
        IntPtr securityAttributes,
        uint creationDisposition,
        uint flagsAndAttributes,
        IntPtr templateFile);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool CloseHandle(IntPtr handle);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool GetFileInformationByHandle(
        IntPtr file,
        out ByHandleFileInformation information);

    [DllImport("ntdll.dll")]
    private static extern uint NtCreateFile(
        out IntPtr fileHandle,
        uint desiredAccess,
        ref ObjectAttributes objectAttributes,
        out IoStatusBlock ioStatusBlock,
        IntPtr allocationSize,
        uint fileAttributes,
        uint shareAccess,
        uint createDisposition,
        uint createOptions,
        IntPtr eaBuffer,
        uint eaLength);

    [DllImport("ntdll.dll")]
    private static extern uint NtSetInformationFile(
        IntPtr fileHandle,
        out IoStatusBlock ioStatusBlock,
        IntPtr fileInformation,
        uint length,
        int fileInformationClass);

    public static void PublishClosure(string artifactDirectory, string[] sourcePaths, string[] expectedHashes)
    {
        if (sourcePaths == null || expectedHashes == null || sourcePaths.Length == 0 ||
            sourcePaths.Length != expectedHashes.Length)
        {
            throw new InvalidOperationException("The GStreamer runtime closure is invalid.");
        }

        using (PinnedDirectoryAncestry ancestry = PinDirectoryAncestryNoFollow(artifactDirectory))
        {
            IntPtr root = ancestry.Final;
            ByHandleFileInformation rootIdentity = ReadInformation(root, artifactDirectory);
            for (int index = 0; index < sourcePaths.Length; index++)
            {
                PublishOne(root, sourcePaths[index], expectedHashes[index]);
            }

            using (PinnedDirectoryAncestry reopened = PinDirectoryAncestryNoFollow(artifactDirectory))
            {
                ByHandleFileInformation reopenedIdentity = ReadInformation(reopened.Final, artifactDirectory);
                if (!SameIdentity(rootIdentity, reopenedIdentity))
                {
                    throw new InvalidOperationException("The artifact directory identity changed during runtime publication.");
                }
            }
        }
    }

    public static void ProvePinnedDirectorySwapCannotRedirect(string directory)
    {
        PinnedDirectoryAncestry ancestry = PinDirectoryAncestryNoFollow(directory);
        IntPtr root = ancestry.Final;
        string moved = directory + ".swap-proof-" + Guid.NewGuid().ToString("N");
        string leaf = ".townlight-retained-root-proof-" + Guid.NewGuid().ToString("N") + ".tmp";
        bool directoryMoved = false;
        bool replacementCreated = false;
        try
        {
            try
            {
                Directory.Move(directory, moved);
            }
            catch (IOException)
            {
                return;
            }
            catch (UnauthorizedAccessException)
            {
                return;
            }
            directoryMoved = true;
            Directory.CreateDirectory(directory);
            replacementCreated = true;

            IntPtr proof;
            uint status = OpenRelative(root, leaf,
                GenericRead | GenericWrite | Delete | Synchronize, FileCreate, out proof);
            if (status != StatusSuccess)
            {
                ThrowNt("The retained-directory swap proof could not create its leaf", status);
            }
            try
            {
                using (var stream = new FileStream(new SafeFileHandle(proof, false), FileAccess.ReadWrite))
                {
                    stream.WriteByte(42);
                    stream.Flush(true);
                }
            }
            finally
            {
                CloseHandle(proof);
            }

            if (!File.Exists(Path.Combine(moved, leaf)) || File.Exists(Path.Combine(directory, leaf)))
            {
                throw new InvalidOperationException("A directory swap redirected a retained-root artifact write.");
            }
        }
        finally
        {
            ancestry.Dispose();
            if (directoryMoved)
            {
                string proofPath = Path.Combine(moved, leaf);
                if (File.Exists(proofPath)) File.Delete(proofPath);
            }
            if (replacementCreated && Directory.Exists(directory)) Directory.Delete(directory, true);
            if (directoryMoved && Directory.Exists(moved)) Directory.Move(moved, directory);
        }
    }

    public static void ProveCheckedTemporaryCleanup(string directory)
    {
        using (PinnedDirectoryAncestry ancestry = PinDirectoryAncestryNoFollow(directory))
        {
            string leaf = ".townlight-cleanup-proof-" + Guid.NewGuid().ToString("N") + ".tmp";
            IntPtr temporary;
            uint status = OpenRelative(ancestry.Final, leaf,
                GenericRead | GenericWrite | Delete | Synchronize, FileCreate, out temporary);
            if (status != StatusSuccess) ThrowNt("The cleanup proof could not create its temporary leaf", status);
            MarkDelete(temporary);
            CloseHandle(temporary);
            IntPtr reopened;
            uint reopenStatus = OpenRelative(ancestry.Final, leaf, GenericRead | Synchronize, FileOpen, out reopened);
            if (reopenStatus == StatusSuccess)
            {
                CloseHandle(reopened);
                throw new InvalidOperationException("A checked temporary cleanup left a stale artifact.");
            }
            if (reopenStatus != StatusObjectNameNotFound)
            {
                ThrowNt("The checked temporary cleanup could not prove absence", reopenStatus);
            }

            bool failureVisible = false;
            try
            {
                MarkDelete(IntPtr.Zero);
            }
            catch (InvalidOperationException)
            {
                failureVisible = true;
            }
            if (!failureVisible)
            {
                throw new InvalidOperationException("A temporary cleanup failure was not visible.");
            }
        }
    }

    private static void PublishOne(IntPtr root, string sourcePath, string expectedHash)
    {
        string leaf = Path.GetFileName(sourcePath);
        if (String.IsNullOrWhiteSpace(leaf) || leaf != Path.GetFileName(leaf) ||
            !expectedHash.Equals(Sha256Path(sourcePath), StringComparison.OrdinalIgnoreCase))
        {
            throw new InvalidOperationException("A GStreamer runtime source failed identity validation.");
        }

        IntPtr source = CreateFileW(sourcePath, GenericRead | Synchronize,
            FileShareRead | FileShareWrite, IntPtr.Zero, OpenExisting,
            FileAttributeNormal | FileFlagOpenReparsePoint, IntPtr.Zero);
        if (source == new IntPtr(-1))
        {
            throw new Win32Exception(Marshal.GetLastWin32Error(), "The GStreamer runtime source could not be pinned.");
        }

        try
        {
            ByHandleFileInformation sourceIdentity = ReadInformation(source, sourcePath);
            if ((sourceIdentity.FileAttributes & FileAttributeReparsePoint) != 0 ||
                (sourceIdentity.FileAttributes & FileAttributeDirectory) != 0)
            {
                throw new InvalidOperationException("A GStreamer runtime source is a reparse point or directory.");
            }

            IntPtr existing;
            uint existingStatus = OpenRelative(root, leaf, GenericRead | Synchronize, FileOpen, out existing);
            if (existingStatus == StatusSuccess)
            {
                try
                {
                    ByHandleFileInformation existingIdentity = ReadInformation(existing, leaf);
                    if ((existingIdentity.FileAttributes & FileAttributeReparsePoint) != 0 ||
                        (existingIdentity.FileAttributes & FileAttributeDirectory) != 0)
                    {
                        throw new InvalidOperationException("A deployed runtime target is a reparse point or directory.");
                    }
                    if (existingIdentity.NumberOfLinks == 1 &&
                        !SameIdentity(sourceIdentity, existingIdentity) &&
                        expectedHash.Equals(Sha256Handle(existing), StringComparison.OrdinalIgnoreCase))
                    {
                        return;
                    }
                }
                finally
                {
                    CloseHandle(existing);
                }
            }
            else if (existingStatus != StatusObjectNameNotFound)
            {
                ThrowNt("The deployed runtime target could not be inspected", existingStatus);
            }

            string temporaryLeaf = "." + leaf + ".townlight-" + Guid.NewGuid().ToString("N") + ".tmp";
            IntPtr temporary;
            uint createStatus = OpenRelative(root, temporaryLeaf,
                GenericRead | GenericWrite | Delete | Synchronize, FileCreate, out temporary);
            if (createStatus != StatusSuccess)
            {
                ThrowNt("The staged runtime DLL could not be created", createStatus);
            }

            bool renamed = false;
            ByHandleFileInformation stagedIdentity;
            try
            {
                using (var sourceStream = new FileStream(new SafeFileHandle(source, false), FileAccess.Read))
                using (var temporaryStream = new FileStream(new SafeFileHandle(temporary, false), FileAccess.ReadWrite))
                {
                    sourceStream.CopyTo(temporaryStream);
                    temporaryStream.Flush(true);
                }
                if (!expectedHash.Equals(Sha256Handle(temporary), StringComparison.OrdinalIgnoreCase))
                {
                    throw new InvalidOperationException("The staged runtime DLL did not verify.");
                }
                stagedIdentity = ReadInformation(temporary, temporaryLeaf);
                if (stagedIdentity.NumberOfLinks != 1 ||
                    (stagedIdentity.FileAttributes & FileAttributeReparsePoint) != 0)
                {
                    throw new InvalidOperationException("The staged runtime DLL is not an independent regular file.");
                }

                RenameRelative(temporary, root, leaf);
                renamed = true;
            }
            finally
            {
                if (!renamed)
                {
                    MarkDelete(temporary);
                }
                CloseHandle(temporary);
            }

            IntPtr verified;
            uint verifiedStatus = OpenRelative(root, leaf, GenericRead | Synchronize, FileOpen, out verified);
            if (verifiedStatus != StatusSuccess)
            {
                ThrowNt("The deployed runtime DLL could not be reopened", verifiedStatus);
            }
            try
            {
                ByHandleFileInformation verifiedIdentity = ReadInformation(verified, leaf);
                if ((verifiedIdentity.FileAttributes & FileAttributeReparsePoint) != 0 ||
                    (verifiedIdentity.FileAttributes & FileAttributeDirectory) != 0 ||
                    verifiedIdentity.NumberOfLinks != 1 ||
                    !SameIdentity(stagedIdentity, verifiedIdentity) ||
                    SameIdentity(sourceIdentity, verifiedIdentity) ||
                    !expectedHash.Equals(Sha256Handle(verified), StringComparison.OrdinalIgnoreCase))
                {
                    throw new InvalidOperationException("The deployed runtime DLL failed final identity verification.");
                }
            }
            finally
            {
                CloseHandle(verified);
            }
        }
        finally
        {
            CloseHandle(source);
        }
    }

    private static PinnedDirectoryAncestry PinDirectoryAncestryNoFollow(string path)
    {
        string fullPath = Path.GetFullPath(path);
        string rootPath = Path.GetPathRoot(fullPath);
        if (String.IsNullOrWhiteSpace(rootPath) || rootPath.StartsWith("\\\\", StringComparison.Ordinal))
        {
            throw new InvalidOperationException("Cargo artifact publication requires a local drive-rooted target path.");
        }

        var handles = new List<IntPtr>();
        try
        {
            IntPtr root = OpenAbsoluteDirectoryNoFollow(rootPath);
            handles.Add(root);
            string remainder = fullPath.Substring(rootPath.Length);
            foreach (string component in remainder.Split(new[] { '\\', '/' }, StringSplitOptions.RemoveEmptyEntries))
            {
                if (component == "." || component == "..")
                {
                    throw new InvalidOperationException("The Cargo artifact path contains an invalid ancestry component.");
                }
                IntPtr child;
                uint status = OpenRelativeObject(handles[handles.Count - 1], component,
                    FileReadAttributes | FileTraverse | Synchronize, FileOpen,
                    FileDirectoryFile | FileFlagOpenReparsePoint | FileSynchronousIoNonAlert,
                    out child);
                if (status != StatusSuccess)
                {
                    ThrowNt("A Cargo artifact ancestry component could not be pinned (" + component + ")", status);
                }
                ByHandleFileInformation information = ReadInformation(child, component);
                if ((information.FileAttributes & FileAttributeReparsePoint) != 0 ||
                    (information.FileAttributes & FileAttributeDirectory) == 0)
                {
                    CloseHandle(child);
                    throw new InvalidOperationException("A Cargo artifact ancestry component is a reparse point or not a directory.");
                }
                handles.Add(child);
            }
            return new PinnedDirectoryAncestry(handles);
        }
        catch
        {
            for (int index = handles.Count - 1; index >= 0; index--) CloseHandle(handles[index]);
            throw;
        }
    }

    private static IntPtr OpenAbsoluteDirectoryNoFollow(string path)
    {
        IntPtr handle = CreateFileW(path, FileReadAttributes | FileTraverse | Synchronize,
            FileShareRead | FileShareWrite, IntPtr.Zero, OpenExisting,
            FileFlagBackupSemantics | FileFlagOpenReparsePoint, IntPtr.Zero);
        if (handle == new IntPtr(-1))
        {
            throw new Win32Exception(Marshal.GetLastWin32Error(), "The artifact directory could not be pinned.");
        }
        ByHandleFileInformation information = ReadInformation(handle, path);
        if ((information.FileAttributes & FileAttributeReparsePoint) != 0 ||
            (information.FileAttributes & FileAttributeDirectory) == 0)
        {
            CloseHandle(handle);
            throw new InvalidOperationException("The artifact directory is a reparse point or not a directory.");
        }
        return handle;
    }

    private static uint OpenRelative(IntPtr root, string leaf, uint access, uint disposition, out IntPtr handle)
    {
        return OpenRelativeObject(root, leaf, access, disposition,
            FileNonDirectoryFile | FileFlagOpenReparsePoint | FileSynchronousIoNonAlert,
            out handle);
    }

    private static uint OpenRelativeObject(
        IntPtr root,
        string leaf,
        uint access,
        uint disposition,
        uint options,
        out IntPtr handle)
    {
        IntPtr buffer = Marshal.StringToHGlobalUni(leaf);
        IntPtr unicodePointer = IntPtr.Zero;
        try
        {
            UnicodeString unicode = new UnicodeString {
                Length = checked((ushort)(leaf.Length * 2)),
                MaximumLength = checked((ushort)(leaf.Length * 2)),
                Buffer = buffer
            };
            unicodePointer = Marshal.AllocHGlobal(Marshal.SizeOf(typeof(UnicodeString)));
            Marshal.StructureToPtr(unicode, unicodePointer, false);
            ObjectAttributes attributes = new ObjectAttributes {
                Length = (uint)Marshal.SizeOf(typeof(ObjectAttributes)),
                RootDirectory = root,
                ObjectName = unicodePointer,
                Attributes = ObjectCaseInsensitive,
                SecurityDescriptor = IntPtr.Zero,
                SecurityQualityOfService = IntPtr.Zero
            };
            IoStatusBlock statusBlock;
            return NtCreateFile(out handle, access, ref attributes, out statusBlock,
                IntPtr.Zero, FileAttributeNormal, FileShareRead | FileShareWrite,
                disposition, options, IntPtr.Zero, 0);
        }
        finally
        {
            if (unicodePointer != IntPtr.Zero) Marshal.FreeHGlobal(unicodePointer);
            Marshal.FreeHGlobal(buffer);
        }
    }

    private static void RenameRelative(IntPtr file, IntPtr root, string destinationLeaf)
    {
        int rootOffset = IntPtr.Size == 8 ? 8 : 4;
        int lengthOffset = rootOffset + IntPtr.Size;
        int nameOffset = lengthOffset + 4;
        byte[] name = System.Text.Encoding.Unicode.GetBytes(destinationLeaf);
        IntPtr information = Marshal.AllocHGlobal(nameOffset + name.Length);
        try
        {
            for (int index = 0; index < nameOffset + name.Length; index++) Marshal.WriteByte(information, index, 0);
            Marshal.WriteByte(information, 0, 1);
            Marshal.WriteIntPtr(information, rootOffset, root);
            Marshal.WriteInt32(information, lengthOffset, name.Length);
            Marshal.Copy(name, 0, IntPtr.Add(information, nameOffset), name.Length);
            IoStatusBlock statusBlock;
            uint status = NtSetInformationFile(file, out statusBlock, information,
                checked((uint)(nameOffset + name.Length)), FileRenameInformation);
            if (status != StatusSuccess) ThrowNt("The staged runtime DLL could not be atomically renamed", status);
        }
        finally
        {
            Marshal.FreeHGlobal(information);
        }
    }

    private static void MarkDelete(IntPtr file)
    {
        IntPtr information = Marshal.AllocHGlobal(1);
        try
        {
            Marshal.WriteByte(information, 1);
            IoStatusBlock statusBlock;
            uint status = NtSetInformationFile(file, out statusBlock, information, 1, FileDispositionInformation);
            if (status != StatusSuccess)
            {
                ThrowNt("The staged runtime DLL could not be marked for checked cleanup", status);
            }
        }
        finally
        {
            Marshal.FreeHGlobal(information);
        }
    }

    private static string Sha256Path(string path)
    {
        using (var stream = new FileStream(path, FileMode.Open, FileAccess.Read,
            FileShare.ReadWrite | FileShare.Delete))
        using (var sha = SHA256.Create())
        {
            return Hex(sha.ComputeHash(stream));
        }
    }

    private static string Sha256Handle(IntPtr handle)
    {
        using (var stream = new FileStream(new SafeFileHandle(handle, false), FileAccess.Read))
        using (var sha = SHA256.Create())
        {
            stream.Position = 0;
            return Hex(sha.ComputeHash(stream));
        }
    }

    private static string Hex(byte[] bytes)
    {
        return BitConverter.ToString(bytes).Replace("-", String.Empty).ToLowerInvariant();
    }

    private static ByHandleFileInformation ReadInformation(IntPtr handle, string description)
    {
        ByHandleFileInformation information;
        if (!GetFileInformationByHandle(handle, out information))
        {
            throw new Win32Exception(Marshal.GetLastWin32Error(), "File identity failed for " + description);
        }
        return information;
    }

    private static bool SameIdentity(ByHandleFileInformation first, ByHandleFileInformation second)
    {
        return first.VolumeSerialNumber == second.VolumeSerialNumber &&
            first.FileIndexHigh == second.FileIndexHigh &&
            first.FileIndexLow == second.FileIndexLow;
    }

    private static void ThrowNt(string message, uint status)
    {
        throw new InvalidOperationException(message + ": NTSTATUS 0x" + status.ToString("x8"));
    }
}
