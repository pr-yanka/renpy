# This code applies an update.

init -1000 python in updater:
    from store import renpy, config, Action
    import store.build as build

    import tarfile
    import threading
    import traceback
    import os
    import urlparse
    import urllib
    import json
    import subprocess
    import hashlib
    import time

    try:
        import rsa
    except:
        pass

    from renpy.exports import fsencode

    class UpdateError(Exception):
        """
        Used to report known errors.
        """

    class UpdateCancelled(Exception):
        """
        Used to report the update being cancelled.
        """

    class Updater(threading.Thread):
        """
        Applies an update.
        
        Fields on this object are used to communicate the state of the update process.
        
        self.state
            The state that the updater is in.
            
        self.message
            In an error state, the error message that occured.
            
        self.progress
            If not None, a number between 0.0 and 1.0 giving some sort of 
            progress indication.
            
        self.can_cancel
            A boolean that indicates if cancelling the update is allowed.
        
        """

        # Here are the possible states.
        
        # An error occured during the update process.
        # self.message is set to the error message.
        ERROR = "ERROR"
        
        # Checking to see if an update is necessary.
        CHECKING = "CHECKING"
        
        # We are up to date. The update process has ended.
        # Calling proceed will return to the main menu.
        UPDATE_NOT_AVAILABLE = "UPDATE NOT AVAILABLE"
        
        # An update is available.
        # The interface should ask the user if he wants to upgrade, and call .proceed()
        # if he wants to continue. 
        UPDATE_AVAILABLE = "UPDATE AVAILABLE"
        
        # Preparing to update by packing the current files into a .update file.
        # self.progress is updated during this process.
        PREPARING = "PREPARING"
        
        # Downloading the update.
        # self.progress is updated during this process.
        DOWNLOADING = "DOWNLOADING"
        
        # Unpacking the update.
        # self.progress is updated during this process.
        UNPACKING = "UNPACKING"
        
        # Finishing up, by moving files around, deleting obsolete files, and writing out
        # the state.
        FINISHING = "FINISHING"
        
        # Done. The update completed successfully.
        # Calling .proceed() on the updater will trigger a game restart.
        DONE = "DONE"
        
        # The update was cancelled.
        CANCELLED = "CANCELLED"
        
        def __init__(self, url, base, force=False, public_key=None, simulate=None):
            """
            Takes the same arguments as update().
            """

            threading.Thread.__init__(self)

            # The main state.
            self.state = Updater.CHECKING
            
            # An additional message to show to the user.
            self.message = None
            
            # The progress of the current operation, or None.
            self.progress = None

            # True if the user can click the cancel button.
            self.can_cancel = True
            
            # True if the user can click the proceed button.
            self.can_proceed = False

            # True if the user has clicked the cancel button.
            self.cancelled = False
            
            # True if the user has clocked the proceed button.
            self.proceeded = False
            
            # The url of the updates.json file.
            self.url = url

            # Force the update?
            self.force = force
            
            # The base path of the game that we're updating, and the path to the update
            # directory underneath it.

            if base is None:
                base = config.basedir

            self.base = os.path.abspath(base)
            self.updatedir = os.path.join(self.base, "update")

            # If we're a mac, the directory in which our app lives.
            splitbase = self.base.split('/')        
            if (len(splitbase) >= 4 and
                splitbase[-1] == "autorun" and
                splitbase[-2] == "Resources" and 
                splitbase[-3] == "Contents" and
                splitbase[-4].endswith(".app")):
            
                self.app = "/".join(splitbase[:-3])
            else:
                self.app = None

            # A condition that's used to coordinate things between the various 
            # threads.
            self.condition = threading.Condition()

            # The modules we'll be updating.
            self.modules = [ ]

            # A list of files that have to be moved into place. This is a list of filenames,
            # where each file is moved from <file>.new to <file>.
            self.moves = [ ]

            # TODO: use renpy.file instead of open.
            if public_key is not None:
                f = renpy.file("public_key")
                self.public_key = rsa.PublicKey.load_pkcs1(f.read())
                f.close()
            else:
                self.public_key = None

            # The logfile that update errors are written to.
            try:
                self.log = open(os.path.join(self.updatedir, "log.txt"), "w")
            except:
                self.log = None

            self.simulate = simulate

            self.daemon = True
            self.start()
     
        
        def run(self):
            """
            The main function of the update thread, handles errors by reporting 
            them to the user.
            """
            
            try:

                if self.simulate:
                    self.simulation()
                else:
                    self.update()
        
            except UpdateCancelled as e:
                self.can_cancel = True
                self.can_proceed = False
                self.progress = None
                self.message = None
                self.state = self.CANCELLED
            
            except UpdateError as e:
                self.message = e.message
                self.can_cancel = True
                self.can_proceed = False
                self.state = self.ERROR
            
            except Exception as e:
                self.message = type(e).__name__ + ": " + unicode(e)
                self.can_cancel = True
                self.can_proceed = False
                self.state = self.ERROR

                if self.log:
                    traceback.print_exc(None, self.log)
                    
            self.clean_old()
            
        def update(self):
            """
            Performs the update.        
            """

            self.load_state()
            self.test_write()
            self.check_updates()
            pretty_version = self.check_versions()

            if not self.modules:
                self.can_cancel = False
                self.can_proceed = True
                self.state = self.UPDATE_NOT_AVAILABLE
                return

            # Confirm with the user that the update is available.
            with self.condition:
                self.can_cancel = True
                self.can_proceed = True
                self.state = self.UPDATE_AVAILABLE
                self.version = pretty_version
                
                while True:
                    if self.cancelled or self.proceeded:
                        break
                    
                    self.condition.wait()

            self.can_proceed = False
            
            if self.cancelled:
                raise UpdateCancelled()
            
            # Perform the update.
            self.new_state = dict(self.current_state)

            self.progress = 0.0
            self.state = self.PREPARING
            
            for i in self.modules:
                self.prepare(i)

            self.progress = 0.0
            self.state = self.DOWNLOADING

            for i in self.modules:
                self.download(i) 

            self.clean_old()

            self.can_cancel = False
            self.progress = 0.0
            self.state = self.UNPACKING
            
            for i in self.modules:
                self.unpack(i) 

            self.progress = None
            self.state = self.FINISHING
            
            self.move_files()
            self.delete_obsolete()
            self.save_state()
            self.clean_new()

            self.message = None
            self.progress = None
            self.can_proceed = True
            self.can_cancel = False
            self.state = self.DONE
            
            return

        def simulation(self):
            """
            Simulates the update.
            """

            def simulate_progress():
                for i in range(0, 30):
                    self.progress = i / 30.0
                    time.sleep(.1)
                    
                    if self.cancelled:
                        raise UpdateCancelled()

            time.sleep(1.5)

            if self.cancelled:
                raise UpdateCancelled()

            if self.simulate == "error":
                raise UpdateError("An error is being simulated.")

            if self.simulate == "not_available":
                self.can_cancel = False
                self.can_proceed = True
                self.state = self.UPDATE_NOT_AVAILABLE
                return

            # Confirm with the user that the update is available.
            with self.condition:
                self.can_cancel = True
                self.can_proceed = True
                self.state = self.UPDATE_AVAILABLE
                self.version = build.version or build.directory_name
                
                while True:
                    if self.cancelled or self.proceeded:
                        break
                    
                    self.condition.wait()
            
            self.can_proceed = False

            if self.cancelled:
                raise UpdateCancelled()
            
            self.progress = 0.0
            self.state = self.PREPARING
            
            simulate_progress()

            self.progress = 0.0
            self.state = self.DOWNLOADING

            simulate_progress()

            self.can_cancel = False
            self.progress = 0.0
            self.state = self.UNPACKING
            
            simulate_progress()

            self.progress = None
            self.state = self.FINISHING
            
            time.sleep(1.5)

            self.message = None
            self.progress = None
            self.can_proceed = True
            self.can_cancel = False
            self.state = self.DONE
            
            return
            
        def proceed(self):
            """
            Causes the upgraded to proceed with the next step in the process.
            """

            if not self.can_proceed:
                return
               
            if self.state == self.UPDATE_NOT_AVAILABLE:
                renpy.full_restart()
            
            elif self.state == self.ERROR:
                renpy.full_restart()
            
            elif self.state == self.CANCELLED:
                renpy.full_restart()

            elif self.state == self.DONE:
                renpy.quit(relaunch=True)

            elif self.state == self.UPDATE_AVAILABLE:
                with self.condition:
                    self.proceeded = True
                    self.condition.notify_all()
            
        def cancel(self):
            
            if not self.can_cancel:
                return
            
            with self.condition:
                self.cancelled = True
                self.condition.notify_all()
                
            renpy.full_restart()
                
        def unlink(self, path):
            """
            Tries to unlink the file at `path`.
            """
            
            if os.path.exists(path + ".old"):
                os.unlink(path + ".old")
                
            if os.path.exists(path):
                os.rename(path, path + ".old")

                # This might fail because of a sharing violation on Windows.            
                try:
                    os.unlink(path + ".old")            
                except:
                    pass
            
            
        def path(self, name):
            """
            Converts a filename to a path on disk.
            """

            if self.app is not None:

                path = name.split("/")
                if path[0].endswith(".app"):
                    rv = os.path.join(self.app, "/".join(path[1:]))
                    return rv

            return os.path.join(self.base, name)
            
                
        def load_state(self):
            """
            Loads the current update state from update/current.json
            """
            
            fn = os.path.join(self.updatedir, "current.json")
            
            if not os.path.exists(fn):
                raise UpdateError("Either this project does not support updating, or the update status file was deleted.")
            
            with open(fn, "r") as f:
                self.current_state = json.load(f)

        def test_write(self):
            fn = os.path.join(self.updatedir, "test.txt")

            try:
                with open(fn, "w") as f:
                    f.write("Hello, World.")
                    
                os.unlink(fn)
            except:
                raise UpdateError("This account does not have permission to perform an update.")

            if not self.log:
                raise UpdateError("This account does not have permission to write the update log.")
            
        def check_updates(self):
            """
            Downloads the list of updates from the server, parses it, and stores it in 
            self.updates.
            """
            
            fn = os.path.join(self.updatedir, "updates.json")
            urllib.urlretrieve(self.url, fn)
            
            with open(fn, "r") as f:
                updates_json = f.read()
                self.updates = json.loads(updates_json)

            if self.public_key is not None:
                fn = os.path.join(self.updatedir, "updates.json.sig")
                urllib.urlretrieve(self.url + ".sig", fn)

                with open(fn, "r") as f:
                    signature = f.read().decode("base64")

                try:
                    rsa.verify(updates_json, signature, self.public_key)
                except:
                    raise UpdateError("Could not verify update signature.")
                

            if "monkeypatch" in self.updates:
                exec self.updates["monkeypatch"] in globals(), globals()
            
        def check_versions(self):
            """
            Decides what modules need to be updated, if any.
            """
            
            rv = None
            
            # A list of names of modules we want to update.
            self.modules = [ ]
            
            # We update the modules that are in both versions, and that are out of date.
            for name, data in self.current_state.iteritems():
                
                if name not in self.updates:
                    continue
                
                if data["version"] == self.updates[name]["version"]:
                    if not self.force:
                        continue
                
                self.modules.append(name)

                rv = data["pretty_version"]
                
            return rv

        def update_filename(self, module, new):
            """
            Returns the update filename for the given module.
            """
            
            rv = os.path.join(self.updatedir, module + ".update")
            if new:
                return rv + ".new"
                
            return rv

        def prepare(self, module):
            """
            Creates a tarfile creating the files that make up module. 
            """
            
            state = self.current_state[module]
            
            xbits = set(state["xbit"])
            directories = set(state["directories"])
            all = state["files"] + state["directories"] 
            all.sort()

            # Add the update directory and state file. 
            all.append("update")
            directories.add("update")
            all.append("update/current.json")

            tf = tarfile.open(self.update_filename(module, False), "w")

            for i, name in enumerate(all):

                if self.cancelled:
                    raise UpdateCancelled()
                
                self.progress = 1.0 * i / len(all)
                
                directory = name in directories
                xbit = name in xbits
                
                path = self.path(name)
                
                if directory:
                    info = tarfile.TarInfo(name)
                    info.size = 0
                    info.type = tarfile.DIRTYPE
                else:
                    if not os.path.exists(path):
                        continue
                    
                    info = tf.gettarinfo(path, name)

                    if not info.isreg():
                        continue
                    
                info.uid = 1000
                info.gid = 1000
                info.mtime = 0
                info.uname = "renpy"
                info.gname = "renpy"
                    
                if xbit or directory:
                    info.mode = 0777
                else:
                    info.mode = 0666
                    
                if info.isreg():
                    with open(path, "rb") as f:
                        tf.addfile(info, f)
                else:
                    tf.addfile(info)
                    
            tf.close()

        def download(self, module):
            """
            Uses zsync to download the module.
            """
            
            start_progress = None
            
            new_fn = self.update_filename(module, True)
            
            cmd = [ 
                "./zsync",
                "-o", new_fn, 
                "-k", os.path.join(self.updatedir, module + ".zsync")
                ]
            
            for i in self.modules:
                cmd.append("-i")
                cmd.append(self.update_filename(module, False))
                
            cmd.append(urlparse.urljoin(self.url, self.updates[module]["url"]))
            
            cmd = [ fsencode(i) for i in cmd ]
            
            self.log.flush()
            p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, bufsize=0)

            while True:

                if self.cancelled:
                    p.kill()
                
                l = p.stdout.readline()
                if not l:
                    break
            
                self.log.write(l)
                
                if l.startswith("PROGRESS "):
                    _, raw_progress = l.split(' ', 1)
                    raw_progress = float(raw_progress) / 100.0
                    
                    if raw_progress == 1.0:
                        self.progress = 1.0
                        continue
                    
                    if start_progress is None:
                        start_progress = raw_progress
                        self.progress = 0.0
                        continue
                    
                    self.progress = (raw_progress - start_progress) / (1.0 - start_progress)
                    
                if l.startswith("ENDPROGRESS"):
                    start_progress = None
                    self.progress = None

            p.wait()

            if self.cancelled:
                raise UpdateCancelled()
            
            # Check the existence of the downloaded file.
            if not os.path.exists(new_fn):
                raise UpdateError("The update file was not downloaded.")
            
            # Check that the downloaded file has the right digest.    
            import hashlib    
            with open(new_fn, "r") as f:
                hash = hashlib.sha256()
                
                while True:
                    data = f.read(1024 * 1024)

                    if not data:
                        break

                    hash.update(data)
                    
                digest = hash.hexdigest()
                
            if digest != self.updates[module]["digest"]:
                raise UpdateError("The update file does not have the correct digest - it may have been corrupted.")

            if self.cancelled:
                raise UpdateCancelled()

        
        def unpack(self, module):
            """
            This unpacks the module. Directories are created immediately, while files are
            created as filename.new, and marked to be moved into position when all packing
            is done.
            """
            
            update_fn = self.update_filename(module, True)

            # First pass, just figure out how many tarinfo objects are in the tarfile.
            tf_len = 0
            tf = tarfile.open(update_fn, "r")
            for i in tf:
                tf_len += 1
            tf.close()
            
            tf = tarfile.open(update_fn, "r")
            
            for i, info in enumerate(tf):

                self.progress = 1.0 * i / tf_len

                if info.name == "update":
                    continue

                # Process the status info for the current module.            
                if info.name == "update/current.json":
                    tff = tf.extractfile(info)
                    state = json.load(tff)
                    tff.close()
                    
                    self.new_state[module] = state[module]
                    
                    continue

                path = self.path(info.name)
                
                # Extract directories.
                if info.isdir():
                    try:
                        os.makedirs(path)
                    except:
                        pass
                
                    continue
                
                if not info.isreg():
                    raise UpdateError("While unpacking {}, unknown type {}.".format(info.name, info.type))

                # Extract regular files.
                tff = tf.extractfile(info)
                new_path = path + ".new"
                f = file(new_path, "wb")
                
                while True:
                    data = tff.read(1024 * 1024)
                    if not data:
                        break
                    f.write(data)
                    
                f.close()
                tff.close()

                if info.mode & 1:
                    # If the xbit is set in the tar info, set it on disk if we can.
                    try:
                        umask = os.umask(0)
                        os.umask(umask)
                        
                        os.chmod(new_path, 0777 & (~umask))
                    except:
                        pass
                    
                self.moves.append(path)

        def move_files(self):
            """
            Move new files into place.
            """
            
            for path in self.moves:
                self.unlink(path)
                os.rename(path + ".new", path)
                
        def delete_obsolete(self):
            """
            Delete files and directories that have been made obsolete by the upgrade. 
            """
            
            def flatten_path(d, key):
                rv = set()
                
                for i in d.itervalues():
                    for j in i[key]:
                        rv.add(self.path(j))
                        
                return rv
                        
            old_files = flatten_path(self.current_state, 'files')
            old_directories = flatten_path(self.current_state, 'directories')  

            new_files = flatten_path(self.new_state, 'files')
            new_directories = flatten_path(self.new_state, 'directories')  
                
            old_files -= new_files
            old_directories -= new_directories
        
            old_files = list(old_files)
            old_files.sort()
            old_files.reverse()
            
            old_directories = list(old_directories)
            old_directories.sort()
            old_directories.reverse()
            
            for i in old_files:
                self.unlink(i)

            for i in old_directories:
                try:
                    os.rmdir(i)
                except:
                    pass
            
        def save_state(self):
            """
            Saves the current state to update/current.json
            """
            
            fn = os.path.join(self.updatedir, "current.json")
            
            with open(fn, "w") as f:
                json.dump(self.new_state, f)
            
        def clean(self, fn):
            """
            Cleans the file named fn from the updates directory.
            """
            
            fn = os.path.join(self.updatedir, fn)
            if os.path.exists(fn):
                try:
                    os.unlink(fn)
                except:
                    pass
                
        def clean_old(self):
            for i in self.modules:
                self.clean(i + ".update")
                
        def clean_new(self):
            for i in self.modules:
                self.clean(i + ".update.new")
                self.clean(i + ".zsync")

    def update(url, base=None, force=False, public_key=None, simulate=None):
        """
        :doc: updater
        
        Updates this Ren'Py game to the latest version.
        
        `url`
            The URL to the updates.json file.
            
        `base`
            The base directory that will be updated. Defaults to the base
            of the current game.
        
        `force`
            Force the update to occur even if the version numbers are 
            the same. (Used for testing.)
            
        `public_key`
            The path to a PEM file containing a public key that the 
            update signature is checked against.
            
        `simulate`
            This is used to test update guis without actually performing
            an update. This can be:
            
            * None to perform an update.
            * "available" to test the case where an update is available.
            * "not_available" to test the case where no update is available.
            * "error" to test an update error.
        """  

        u = Updater(url=url, base=base, force=force, public_key=public_key, simulate=simulate)        
        ui.timer(.1, repeat=True, action=renpy.restart_interaction)        
        renpy.call_screen("updater", u=u)
    
    class Update(Action):
        """
        :doc: updater
        
        An action that calls :func:`updater.update`. All arguments are
        stored and passed to that function.
        """
        
        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs
            
        def __call__(self):
            renpy.invoke_in_new_context(update, *args, **kwargs)
            
    def update_command():
        import time

        ap = renpy.argument.ArgumentParser()
       
        ap.add_argument("url")
        ap.add_argument("--base", action=store, help="The base directory of the game to update. Defaults to the current game.")
        ap.add_argument("--force", action="store_true", help="Force the update to run even if the version numbers are the same.")
        ap.add_argument("--key", action="store_true", help="A file giving the public key to use of the update.")
        ap.add_argument("--simulate", help="The simulation mode to use. One of available, not_available, or error.")
       
        args = ap.parse_args()
       
        u = Updater(args.url, args.base, args.force, public_key=args.key, simulate=args.simulate)

        while True:
            
            state = u.state
            
            print "State:", state
            
            if u.progress:
                print "Progress: {:.1f}%".format(u.progress * 100.0)
            
            if u.message:
                print "Message:", u.message
                
            if state == u.ERROR:
                break
            elif state == u.UPDATE_NOT_AVAILABLE:
                break
            elif state == u.UPDATE_AVAILABLE:
                u.proceed()
            elif state == u.DONE:
                break
            elif state == u.CANCELLED:
                break
            
            time.sleep(.1)

        return False
    
    renpy.arguments.register_command("update", update_command)

init -1000:
    screen updater:
        
        add "#000"
        
        frame:
            style_group ""
            
            xalign .5
            ypos 100
            xpadding 20
            ypadding 20
    
            xmaximum 400
            xfill True
  
            has vbox
            
            label _("Updater")

            null height 10

            if u.state == u.ERROR:
                text _("An error has occured:")
            elif u.state == u.CHECKING:
                text _("Checking for updates.")
            elif u.state == u.UPDATE_NOT_AVAILABLE:
                text _("This program is up to date.")
            elif u.state == u.UPDATE_AVAILABLE:
                text _("[u.version] is available. Do you want to install it?")
            elif u.state == u.PREPARING:
                text _("Preparing to download the update.")
            elif u.state == u.DOWNLOADING:
                text _("Downloading the update.")
            elif u.state == u.UNPACKING:
                text _("Unpacking the update.")
            elif u.state == u.FINISHING:
                text _("Finishing up.")
            elif u.state == u.DONE:
                text _("The update has been installed. The program will now restart.")
            elif u.state == u.CANCELLED:
                text _("The update was cancelled.")
                
            if u.message is not None:
                null height 10
                text "[u.message!q]"
                
            if u.progress is not None:
                null height 10
                bar value u.progress range 1.0 style "_bar"

            if u.can_proceed or u.can_cancel:
                null height 10

            if u.can_proceed:
                textbutton _("Proceed") action u.proceed xfill True
                
            if u.can_cancel:
                textbutton _("Cancel") action u.cancel xfill True
                
                    
                
            
                
            
            
        
        