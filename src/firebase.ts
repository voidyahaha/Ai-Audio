import { initializeApp } from 'firebase/app'
import { getAuth, GoogleAuthProvider } from 'firebase/auth'
import { getFirestore } from 'firebase/firestore'
import { getStorage } from 'firebase/storage'

const firebaseConfig = {
  apiKey: "AIzaSyDlCAyM6jSkpa2JIXcn3GzkOgY2GAymdNI",
  authDomain: "vocal-separater-5eb7c.firebaseapp.com",
  projectId: "vocal-separater-5eb7c",
  storageBucket: "vocal-separater-5eb7c.firebasestorage.app",
  messagingSenderId: "40754063592",
  appId: "1:40754063592:web:de554c96600b9cfdbf815d"
}

const app = initializeApp(firebaseConfig)

export const auth = getAuth(app)
export const db = getFirestore(app)
export const storage = getStorage(app)
export const googleProvider = new GoogleAuthProvider()

googleProvider.setCustomParameters({
  prompt: 'select_account'
})

export default app
