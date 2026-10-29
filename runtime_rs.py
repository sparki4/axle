"""Rust support code that AXLE adds to generated programs only when a feature is used."""

STR = r'''
#[derive(Clone)]
enum AxleStr { S(&'static str), H(std::sync::Arc<str>) }
impl std::ops::Deref for AxleStr {
    type Target = str;
    fn deref(&self) -> &str { match self { AxleStr::S(s) => s, AxleStr::H(s) => s } }
}
impl PartialEq for AxleStr { fn eq(&self, other: &Self) -> bool { **self == **other } }
impl std::fmt::Display for AxleStr {
    fn fmt(&self, f: &mut std::fmt::Formatter) -> std::fmt::Result { f.write_str(self) }
}
impl AxleStr { fn from_string(s: String) -> AxleStr { AxleStr::H(s.into()) } }
fn axle_str_byte(s: AxleStr, i: u64) -> u64 {
    let i = usize::try_from(i).expect("index too large");
    *s.as_bytes().get(i).unwrap_or_else(|| panic!("str_byte index {} is out of bounds for length {}", i, s.len())) as u64
}
fn axle_str_slice(s: AxleStr, a: u64, b: u64) -> AxleStr {
    let (a, b) = (usize::try_from(a).expect("index too large"), usize::try_from(b).expect("index too large"));
    match s.get(a..b) {
        Some(t) => AxleStr::from_string(t.to_string()),
        None => panic!("str_slice range {}..{} is out of bounds or not on a UTF-8 boundary", a, b),
    }
}
fn axle_str_find(h: AxleStr, n: AxleStr) -> i64 { match h.find(&*n) { Some(i) => i as i64, None => -1 } }
'''

CONC = r'''
struct AxleTaskCell<T> { handle: std::cell::RefCell<Option<std::thread::JoinHandle<T>>> }
impl<T> Drop for AxleTaskCell<T> {
    fn drop(&mut self) { if let Some(h) = self.handle.get_mut().take() { let _ = h.join(); } }
}
type AxleTask<T> = std::rc::Rc<AxleTaskCell<T>>;
fn axle_spawn<T: Send + 'static>(f: impl FnOnce() -> T + Send + 'static) -> AxleTask<T> {
    std::rc::Rc::new(AxleTaskCell { handle: std::cell::RefCell::new(Some(std::thread::spawn(f))) })
}
fn axle_join<T>(task: AxleTask<T>) -> T {
    let handle = task.handle.borrow_mut().take().unwrap_or_else(|| panic!("task was already joined"));
    match handle.join() { Ok(v) => v, Err(_) => panic!("a spawned task panicked") }
}
struct AxleChanInner { state: std::sync::Mutex<(std::collections::VecDeque<u64>, bool)>, ready: std::sync::Condvar }
type AxleChan = std::sync::Arc<AxleChanInner>;
fn axle_chan_new() -> AxleChan {
    std::sync::Arc::new(AxleChanInner { state: std::sync::Mutex::new((std::collections::VecDeque::new(), false)), ready: std::sync::Condvar::new() })
}
fn axle_chan_send(c: AxleChan, v: u64) {
    let mut g = c.state.lock().unwrap();
    if g.1 { panic!("send on a closed channel"); }
    g.0.push_back(v);
    c.ready.notify_one();
}
fn axle_chan_recv(c: AxleChan, fallback: u64) -> u64 {
    let mut g = c.state.lock().unwrap();
    loop {
        if let Some(v) = g.0.pop_front() { return v; }
        if g.1 { return fallback; }
        g = c.ready.wait(g).unwrap();
    }
}
fn axle_chan_close(c: AxleChan) {
    let mut g = c.state.lock().unwrap();
    g.1 = true;
    c.ready.notify_all();
}
'''

ALLOC = [
    'fn axle_layout<T>(count: u64) -> std::alloc::Layout { assert!(count > 0, "allocation count must be positive"); std::alloc::Layout::array::<T>(usize::try_from(count).expect("allocation too large")).expect("allocation too large") }',
    'fn axle_alloc<T>(count: u64) -> *mut T { let layout = axle_layout::<T>(count); let pointer = unsafe { std::alloc::alloc_zeroed(layout) }; if pointer.is_null() { std::alloc::handle_alloc_error(layout); } pointer.cast::<T>() }',
    'unsafe fn axle_free<T>(pointer: *mut T, count: u64) { std::alloc::dealloc(pointer.cast::<u8>(), axle_layout::<T>(count)); }',
]
