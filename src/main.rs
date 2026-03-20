use std::ffi::{CStr, CString};
use std::os::raw::{c_char, c_int};
use std::ptr;

#[repr(C)]
pub struct FILE;

// ---- GRASS / libc FFI (match these to your existing bindings) ----
extern "C" {
    fn G_mapset() -> *const c_char;
    fn G_fopen_old(element: *const c_char, name: *const c_char, mapset: *const c_char) -> *mut FILE;
    fn G_mapset_permissions(mapset: *const c_char) -> c_int;

    fn fscanf(stream: *mut FILE, format: *const c_char, ...) -> c_int;
    fn fclose(stream: *mut FILE) -> c_int;
}

// ---- Your existing state / helpers (assumed to exist) ----
extern "C" {
    // If new_mapset is C, keep this; if it's Rust, replace with a Rust fn.
    fn new_mapset(name: *const c_char);
}

// Adjust to your actual GNAME_MAX from GRASS headers
const GNAME_MAX: usize = 256;

// Example of the fields the C code touches; adapt to your real `st` definition.
#[repr(C)]
pub struct PathState {
    pub count: usize,
    pub size: usize,
    pub names: *mut *mut c_char,
}

#[repr(C)]
pub struct State {
    pub path: PathState,
}

// Your global `st` from C (or your port)
extern "C" {
    static mut st: State;
}

#[no_mangle]
pub unsafe extern "C" fn G__get_list_of_mapsets() {
    let st_ref: &mut State = &mut st;

    if st_ref.path.count > 0 {
        return;
    }

    st_ref.path.count = 0;
    st_ref.path.size = 0;
    st_ref.path.names = ptr::null_mut();

    let cur_ptr = G_mapset();
    if cur_ptr.is_null() {
        return;
    }

    // new_mapset(cur);
    new_mapset(cur_ptr);

    // fp = G_fopen_old("", "SEARCH_PATH", G_mapset());
    let element = CString::new("").unwrap();
    let search_path = CString::new("SEARCH_PATH").unwrap();
    let fp = G_fopen_old(element.as_ptr(), search_path.as_ptr(), cur_ptr);

    if !fp.is_null() {
        let mut name_buf = [0 as c_char; GNAME_MAX];
        let fmt = CString::new("%s").unwrap();

        while fscanf(fp, fmt.as_ptr(), name_buf.as_mut_ptr()) == 1 {
            // if (strcmp(name, cur) == 0) continue;
            if CStr::from_ptr(name_buf.as_ptr()).to_bytes() == CStr::from_ptr(cur_ptr).to_bytes() {
                continue;
            }

            // if (G_mapset_permissions(name) >= 0) new_mapset(name);
            if G_mapset_permissions(name_buf.as_ptr()) >= 0 {
                new_mapset(name_buf.as_ptr());
            }
        }

        fclose(fp);
    } else {
        // static const char perm[] = "PERMANENT";
        let perm = CString::new("PERMANENT").unwrap();

        // if (strcmp(perm, cur) != 0 && G_mapset_permissions(perm) >= 0) new_mapset(perm);
        if CStr::from_ptr(perm.as_ptr()).to_bytes() != CStr::from_ptr(cur_ptr).to_bytes()
            && G_mapset_permissions(perm.as_ptr()) >= 0
        {
            new_mapset(perm.as_ptr());
        }
    }
}
