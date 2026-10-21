import { useState, useEffect, useCallback } from 'react'
import {
  signInWithEmailAndPassword,
  createUserWithEmailAndPassword,
  signInWithPopup,
  signOut,
  onAuthStateChanged,
  updateProfile,
  sendPasswordResetEmail,
  User as FirebaseUser,
} from 'firebase/auth'
import { doc, setDoc, getDoc, serverTimestamp } from 'firebase/firestore'
import { auth, db, googleProvider } from '../firebase'
import { useAppStore } from '../store'
import { User } from '../types'
import toast from 'react-hot-toast'

export function useAuth() {
  const [loading, setLoading] = useState(true)
  const [authError, setAuthError] = useState<string | null>(null)
  const { user, setUser } = useAppStore()

  const createUserDoc = useCallback(async (firebaseUser: FirebaseUser) => {
    try {
      const userRef = doc(db, 'users', firebaseUser.uid)
      const snap = await getDoc(userRef)

      if (!snap.exists()) {
        await setDoc(userRef, {
          uid: firebaseUser.uid,
          email: firebaseUser.email,
          displayName: firebaseUser.displayName,
          photoURL: firebaseUser.photoURL,
          createdAt: serverTimestamp(),
          totalJobs: 0,
          totalProcessingTime: 0,
          plan: 'free',
        })
      }

      const userData: User = {
        uid: firebaseUser.uid,
        email: firebaseUser.email,
        displayName: firebaseUser.displayName,
        photoURL: firebaseUser.photoURL,
        createdAt: snap.exists() ? snap.data().createdAt?.toDate() || new Date() : new Date(),
        totalJobs: snap.exists() ? snap.data().totalJobs || 0 : 0,
        totalProcessingTime: snap.exists() ? snap.data().totalProcessingTime || 0 : 0,
        plan: snap.exists() ? snap.data().plan || 'free' : 'free',
      }

      setUser(userData)
      return userData
    } catch (err) {
      console.warn('Could not sync user profile to Firestore:', err)
      const fallbackUser: User = {
        uid: firebaseUser.uid,
        email: firebaseUser.email,
        displayName: firebaseUser.displayName,
        photoURL: firebaseUser.photoURL,
        createdAt: new Date(),
        totalJobs: 0,
        totalProcessingTime: 0,
        plan: 'free',
      }
      setUser(fallbackUser)
      return fallbackUser
    }
  }, [setUser])

  useEffect(() => {
    const savedGuest = localStorage.getItem('stemai_guest_user')
    if (savedGuest) {
      try {
        const guestData = JSON.parse(savedGuest)
        guestData.createdAt = new Date(guestData.createdAt)
        setUser(guestData)
        setLoading(false)
        return
      } catch {
        localStorage.removeItem('stemai_guest_user')
      }
    }

    const unsubscribe = onAuthStateChanged(auth, async (firebaseUser: any) => {
      if (localStorage.getItem('stemai_guest_user')) {
        setLoading(false)
        return
      }
      if (firebaseUser) {
        await createUserDoc(firebaseUser)
      } else {
        setUser(null)
      }
      setLoading(false)
    })
    return unsubscribe
  }, [createUserDoc, setUser])

  const signInEmail = useCallback(async (email: string, password: string) => {
    setAuthError(null)
    try {
      setLoading(true)
      await signInWithEmailAndPassword(auth, email, password)
      toast.success('Welcome back!')
    } catch (e: any) {
      console.error('Sign in error:', e)
      const msg = getFirebaseErrorMessage(e.code, e.message)
      setAuthError(msg)
      toast.error(msg)
      throw e
    } finally {
      setLoading(false)
    }
  }, [])

  const signUpEmail = useCallback(
    async (email: string, password: string, displayName: string) => {
      setAuthError(null)
      try {
        setLoading(true)
        const { user: firebaseUser } = await createUserWithEmailAndPassword(auth, email, password)
        await updateProfile(firebaseUser, { displayName })
        await createUserDoc(firebaseUser)
        toast.success('Account created! Welcome to StemAI 🎵')
      } catch (e: any) {
        console.error('Sign up error:', e)
        const msg = getFirebaseErrorMessage(e.code, e.message)
        setAuthError(msg)
        toast.error(msg)
        throw e
      } finally {
        setLoading(false)
      }
    },
    [createUserDoc]
  )

  const signInGoogle = useCallback(async () => {
    setAuthError(null)
    try {
      setLoading(true)
      await signInWithPopup(auth, googleProvider)
      toast.success('Signed in with Google!')
    } catch (e: any) {
      console.error('Google sign in error:', e)
      if (e.code !== 'auth/popup-closed-by-user') {
        const msg = getFirebaseErrorMessage(e.code, e.message)
        setAuthError(msg)
        toast.error(msg)
      }
      throw e
    } finally {
      setLoading(false)
    }
  }, [])

  const logout = useCallback(async () => {
    localStorage.removeItem('stemai_guest_user')
    try {
      await signOut(auth)
    } catch {
      // ignore sign out error
    }
    setUser(null)
    toast.success('Signed out')
  }, [setUser])

  const resetPassword = useCallback(async (email: string) => {
    try {
      await sendPasswordResetEmail(auth, email)
      toast.success('Password reset email sent!')
    } catch (e: any) {
      const msg = getFirebaseErrorMessage(e.code)
      toast.error(msg)
      throw e
    }
  }, [])

  const continueAsGuest = useCallback(() => {
    const guestUser: User = {
      uid: 'demo-user-123',
      email: 'demo@stemai.local',
      displayName: 'Demo Creator',
      photoURL: null,
      createdAt: new Date(),
      totalJobs: 0,
      totalProcessingTime: 0,
      plan: 'free',
    }
    localStorage.setItem('stemai_guest_user', JSON.stringify(guestUser))
    setUser(guestUser)
    toast.success('Welcome! Entered Demo / Guest mode 🎵')
  }, [setUser])

  return {
    user,
    loading,
    authError,
    signInEmail,
    signUpEmail,
    signInGoogle,
    logout,
    resetPassword,
    continueAsGuest,
    setAuthError,
  }
}

function getFirebaseErrorMessage(code: string, rawMessage?: string): string {
  const messages: Record<string, string> = {
    'auth/user-not-found': 'No account found with this email',
    'auth/wrong-password': 'Incorrect password',
    'auth/invalid-credential': 'Invalid email or password',
    'auth/email-already-in-use': 'An account with this email already exists',
    'auth/weak-password': 'Password must be at least 6 characters',
    'auth/invalid-email': 'Please enter a valid email address',
    'auth/operation-not-allowed': 'Email/Password sign-in is not enabled in Firebase Console',
    'auth/api-key-not-valid.-please-pass-a-valid-api-key.': 'Firebase API key is invalid. Please check VITE_FIREBASE_API_KEY',
    'auth/too-many-requests': 'Too many failed attempts. Please try again later',
    'auth/network-request-failed': 'Network error. Check your connection',
    'auth/popup-blocked': 'Popup blocked. Please allow popups for this site',
    'auth/popup-closed-by-user': 'Sign-in popup was closed',
    'auth/cancelled-popup-request': 'Sign-in was cancelled',
    'permission-denied': 'Firestore permission denied. Check your Firestore security rules',
  }
  return messages[code] || rawMessage || (code ? `Auth error: ${code}` : 'Authentication error. Please try again.')
}
